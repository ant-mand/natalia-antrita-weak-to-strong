import itertools
import os
import pickle
import time
from dataclasses import dataclass
from typing import Callable, Optional

import datasets
import numpy as np
import torch
import torch_optimizer as toptim
from transformers.modeling_utils import load_sharded_checkpoint
from torch.utils.data import DataLoader
from safetensors.torch import load_model

import weak_to_strong.logger as logger
from weak_to_strong.common import clear_mem
from weak_to_strong.eval import eval_model_acc
from weak_to_strong.loss import xent_loss, logconf_loss_fn
from weak_to_strong.model import TransformerWithHead


@dataclass
class ModelConfig:
    name: str
    default_lr: float
    eval_batch_size: int
    custom_kwargs: Optional[dict] = None
    gradient_checkpointing: bool = False
    model_parallel: bool = False
    default_optimizer: str = "adam"

def pad_collate(batch):
    """
    Custom collate function to pad sequences to the same length within a batch.
    """
    input_ids = [torch.tensor(item["input_ids"]) for item in batch]
    soft_labels = [torch.tensor(item["soft_label"]) for item in batch]

    padded_input_ids = torch.nn.utils.rnn.pad_sequence(input_ids, batch_first=True)
    padded_soft_labels = torch.nn.utils.rnn.pad_sequence(soft_labels, batch_first=True)

    return {"input_ids": padded_input_ids, "soft_label": padded_soft_labels}

def initialize_csv(file_path):
    """ initialize csv file """
    with open(file_path, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(["step", "progress", "train_loss", "train_accuracy", "validation_loss", "test_loss", "lr"])

def write_to_csv(file_path, step, progress, train_loss, train_accuracy, validation_loss, test_loss, lr):
    """ write to a csv file """
    with open(file_path, mode='a', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([step, progress, loss, train_accuracy, validation_loss, test_loss, lr])

def train_model(
    model: torch.nn.Module,
    ds: datasets.Dataset,
    batch_size: int,
    lr: float = 5e-05,
    loss_fn: Callable = xent_loss,
    log_every: int = 10,
    eval_every: int = 100,
    eval_batch_size: int = 256,
    minibatch_size: int = 8,
    eval_ds: Optional[datasets.Dataset] = None,  
    test_ds: Optional[datasets.Dataset] = None,   
    gradient_checkpointing: bool = False,
    train_with_dropout: bool = False,
    epochs: int = 1,
    lr_schedule: str = "cosine_anneal",
    optimizer_name: str = "adam",
    csv_path: Optional[str] = None
):
    print("LR", lr, "batch_size", batch_size, "minibatch_size", minibatch_size)
    assert batch_size % minibatch_size == 0, "batch size must be divisible by minibatch size"
    
    if csv_path is not None:
        initialize_csv(csv_path)  # initialize csv with csv path

    if train_with_dropout:
        model.train()
    else:
        model.eval()
    
    if gradient_checkpointing:
        model.gradient_checkpointing_enable() if hasattr(model, "gradient_checkpointing_enable") else model.module.gradient_checkpointing_enable()

    nsteps = len(ds) * epochs // batch_size

    def lr_schedule_fn(step):
        if lr_schedule == "constant":
            return 1
        else:
            assert False, f"invalid lr schedule, {lr_schedule}, must be constant or cosine_anneal"

    if optimizer_name.lower() == "adam":
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    elif optimizer_name.lower() == "adafactor":
        optimizer = toptim.Adafactor(model.parameters(), lr=lr)
    else:
        assert False, f"invalid optimizer {optimizer_name}, must be adam or adafactor"
    
    if lr_schedule == "cosine_anneal":
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, nsteps)
    else:
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_schedule_fn)
    
    step = 0
    it = itertools.chain.from_iterable(itertools.repeat(ds, epochs))
    losses = []
    accuracies = []
    eval_acc_dict = {}

    io_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(io_device)

    def calculate_loss(model, dataset, loss_fn, batch_size, step_frac=0):
      if dataset is None or len(dataset) == 0:
        return float('nan')

      model.eval()
      dataloader = DataLoader(dataset, batch_size=batch_size, collate_fn=pad_collate)
      total_loss = 0
      count = 0

      with torch.no_grad():
        for batch in dataloader:
          input_ids = batch["input_ids"].to(io_device)
          labels = batch["soft_label"].to(io_device)
          outputs = model(input_ids)
          loss = loss_fn(outputs, labels, step_frac)
          total_loss += loss.item()
          count += 1
      return total_loss / count if count > 0 else float('nan')

    # Initialize val_loss and test_loss to None
    val_loss, test_loss = None, None

    while step < nsteps:
        loss_tot = 0
        all_logits = []
        all_labels = []
        for i in range(batch_size // minibatch_size):
            try:
                mbatch = [next(it) for _ in range(minibatch_size)]
            except StopIteration:
                break
            input_ids = torch.nn.utils.rnn.pad_sequence(
                [torch.tensor(ex["input_ids"]) for ex in mbatch], batch_first=True
            ).to(io_device)
            labels = torch.tensor([ex["soft_label"] for ex in mbatch]).to(io_device)

            logits = model(input_ids)

            all_logits.extend(logits.to(io_device))
            all_labels.extend(labels)

        all_logits = torch.stack(all_logits)
        all_labels = torch.stack(all_labels)
        loss = loss_fn(all_logits, all_labels, step_frac=step / nsteps)
        loss_tot += loss.item()
        loss.backward()
        losses.append(loss_tot)
        accuracies.append(
            torch.mean(
                (torch.argmax(all_logits, dim=1) == torch.argmax(all_labels, dim=1)).to(torch.float32)
            ).item()
        )
        logger.logkvs(
            {
                "step": step,
                "progress": step / nsteps,
                "loss": loss_tot,
                "train_accuracy": accuracies[-1],
                "lr": lr_scheduler.get_last_lr()[0],
            }
        )
        optimizer.step()
        optimizer.zero_grad()
        lr_scheduler.step()

        if log_every and step % log_every == 0:
            # Calculate and print validation and test losses
            if eval_ds is not None:
                val_loss = calculate_loss(model, eval_ds, loss_fn, eval_batch_size, step_frac=step / nsteps)
            if test_ds is not None:
                test_loss = calculate_loss(model, test_ds, loss_fn, eval_batch_size, step_frac=step / nsteps)

            print(
                f"Step: {step}/{nsteps} Recent training losses: {np.mean(losses)} {np.mean(accuracies)} {len(losses)}"
            )
            if val_loss is not None:
                print(f"Step: {step}/{nsteps} Recent validation losses: {val_loss}")
            if test_loss is not None:
                print(f"Step: {step}/{nsteps} Recent test losses: {test_loss}")

            if csv_path is not None:
                write_to_csv(csv_path, step, step / nsteps, np.mean(losses), np.mean(accuracies), val_loss, test_loss, lr_scheduler.get_last_lr()[0])

            losses = []
            accuracies = []
        
        step += 1
        logger.dumpkvs()
        torch.cuda.empty_cache()

    final_eval_results = None
    if test_ds is not None:
        print("Final evaluation (test_ds):")
        final_eval_results = eval_model_acc(model, test_ds, eval_batch_size)
        logger.logkv("test_accuracy", np.mean([r["acc"] for r in final_eval_results]))
        logger.dumpkvs()
    
    return final_eval_results
    

def train_and_save_model(
    model_config: ModelConfig,
    train_ds: datasets.Dataset,
    test_ds: datasets.Dataset,
    inference_ds: Optional[datasets.Dataset] = None,
    *,
    batch_size: int,
    lr: float,
    epochs: int,
    eval_batch_size: Optional[int] = None,
    minibatch_size_per_device: Optional[int] = None,
    save_path: Optional[str] = None,
    loss_fn: Callable = xent_loss,
    label: str = "default",
    force_retrain: bool = False,
    train_with_dropout: bool = False,
    linear_probe: bool = False,
    lr_schedule: str = "constant",
    optimizer_name: str = "adam",
    eval_every: Optional[int] = None,
    csv_path: Optional[str] = None
):
    if eval_batch_size is None:
        eval_batch_size = batch_size

    if minibatch_size_per_device is None:
        minibatch_size_per_device = 1

    gradient_checkpointing = model_config.gradient_checkpointing
    custom_kwargs = model_config.custom_kwargs or {}

    def maybe_load_model(model):
        print("Save path: {}".format(save_path))
        if os.path.exists(os.path.join(save_path, "results.txt")) and not force_retrain:
            print("loading from", save_path)
            checkpoint_path = os.path.join(save_path, "model.safetensors")
            try:
                if not os.path.exists(checkpoint_path):
                    print("using load_sharded_checkpoint")
                    load_sharded_checkpoint(model, checkpoint_path)
                else:
                    print("using load_model")
                    state_dict = torch.load(checkpoint_path)
                    model.load_state_dict(state_dict)
                model.to("cuda")  # Ensure the model is on the correct device
                return True
            except Exception as e:
                print(f"error loading model: {e}")
                return False
        return False
    
    already_trained = False
    # Load the model
    if model_config.model_parallel:
        assert torch.cuda.device_count() > 1, f"you might want more gpus for {model_config.name}"
        model = TransformerWithHead.from_pretrained(
            model_config.name,
            num_labels=2,
            device_map="auto",
            linear_probe=linear_probe,
            **custom_kwargs,
        )
        model.to("cuda")
        already_trained = maybe_load_model(model)
        # slight misnomer, more like minibatch_size_per_dp_replica
        minibatch_size = minibatch_size_per_device
    else:
        model = TransformerWithHead.from_pretrained(
            model_config.name, num_labels=2, linear_probe=linear_probe, **custom_kwargs
        ).to("cuda")
        already_trained = maybe_load_model(model)
        # data parallel:  currently not supported with model parallel

        minibatch_size = min(minibatch_size_per_device * torch.cuda.device_count(), batch_size)

        if torch.cuda.device_count() > 1:
            model = torch.nn.DataParallel(model, output_device=0)
            print(
                "Using",
                torch.cuda.device_count(),
                "GPUs, setting minibatch_size to",
                minibatch_size,
            )
        else:
            minibatch_size = minibatch_size_per_device

    if already_trained:
        test_results = eval_model_acc(model, test_ds, eval_batch_size)
    else:
        start = time.time()
        test_results = train_model(
            model,
            train_ds,
            batch_size,
            lr=lr,
            epochs=epochs,
            eval_ds=inference_ds,
            test_ds=test_ds,
            gradient_checkpointing=gradient_checkpointing,
            loss_fn=loss_fn,
            eval_batch_size=eval_batch_size,
            eval_every=eval_every,
            minibatch_size=minibatch_size,
            train_with_dropout=train_with_dropout,
            lr_schedule=lr_schedule,
            optimizer_name=optimizer_name,
            csv_path=csv_path
        )
        print("Model training took", time.time() - start, "seconds")
        if save_path:
            # Note: If the model is wrapped by DataParallel, we need to unwrap it before saving
            (model if hasattr(model, "save_pretrained") else model.module).save_pretrained(
                save_path, safe_serialization=False
            )
            print("saved", save_path)

    inference_results = None
    if inference_ds:
        print("Inference accuracy:")
        inference_results = eval_model_acc(model, inference_ds, eval_batch_size)
        logger.logkv("inference_accuracy", np.mean([r["acc"] for r in inference_results]))

    if save_path:
        with open(os.path.join(save_path, "results.pkl"), "wb") as f:
            pickle.dump(
                {
                    "avg_acc_test": float(np.mean([r["acc"] for r in test_results])),
                    "avg_acc_inference": float(
                        np.mean([r["acc"] for r in inference_results] if inference_results else [])
                    ),
                    "test_results": test_results,
                    "inference_results": inference_results if inference_results else [],
                },
                f,
            )
    # try to clean up memory
    clear_mem()
    logger.shutdown()

    return test_results, inference_results
