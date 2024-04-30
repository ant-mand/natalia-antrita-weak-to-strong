#!/bin/sh

MODEL_SIZE="gpt2"
DATASET_NAME="boolq"
BATCH_SIZE=32
LEARNING_RATE=1e-5
EPOCHS=3
SWEEP_SUBFOLDER="boolq_experiment_01"
LOSS_TYPE="logconf"
MODEL_CKPT="gpt2-boolq"
STRONG_CKPT_PATH="./results/gpt2-medium/bs=32-dn=boolq-e=3-l=xent-l=1e-05-ms=gpt2-medium/model.safetensors"
WEAK_LABELS_PATH="./results/gpt2/bs=32-dn=boolq-e=3-l=xent-l=1e-05-ms=gpt2/weak_labels"

python train_simple.py \
    --model_size=$MODEL_SIZE \
    --ds_name=$DATASET_NAME \
    --batch_size=$BATCH_SIZE \
    --lr=$LEARNING_RATE \
    --epochs=$EPOCHS \
    --sweep_subfolder=$SWEEP_SUBFOLDER \
    --loss=$LOSS_TYPE \
    --model_ckpt=$MODEL_CKPT \
    --strong_ckpt_path=$STRONG_CKPT_PATH
