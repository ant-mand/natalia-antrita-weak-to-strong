import functools
from dataclasses import dataclass
from random import Random
from typing import Any, Callable, Optional

from datasets import Dataset as HfDataset
from datasets import load_dataset as hf_load_dataset


@dataclass
class DatasetConfig:
    # split -> unshuffled dataset of items
    loader: Callable[[str], HfDataset]
    # formats items to have keys 'txt' and 'hard_label', takes a random.Random rng
    formatter: Callable[[Any], Any]


# mapping from dataset name to load function and format function
_REGISTRY: dict[str, DatasetConfig] = {}


def register_dataset(name: str, config: DatasetConfig):
    _REGISTRY[name] = config


def load_dataset(ds_name: str, seed: int = 0, split_sizes: Optional[dict] = None):
    if split_sizes is None:
        split_sizes = dict(train=None, test=None)

    if ds_name not in _REGISTRY:
        raise ValueError(f"Unknown dataset {ds_name}, please register")
    cfg = _REGISTRY[ds_name]
    results = {}
    for split, n_docs in split_sizes.items():
        ds = cfg.loader(split)
        try:
            ds = ds.select(range(n_docs))
        except IndexError as e:
            print(f"Warning {ds_name} has less than {n_docs} docs, using all: {e}")
        ds = ds.map(functools.partial(cfg.formatter, rng=Random(seed)))
        ds = ds.map(
            lambda ex: {"soft_label": [1 - float(ex["hard_label"]), float(ex["hard_label"])]}
        )
        ds = ds.shuffle(seed=seed)  # shuffling a bit pointless for test set but wtv
        results[split] = ds
    return results


def tokenize_dataset(
    raw_ds: HfDataset,
    tokenizer: Callable,
    max_ctx: int,
):
    """
    This function prepares the dataset for training. It takes the raw dataset, a formatting function,
    a tokenizer, a maximum context length

    Parameters:
    raw_ds: The raw dataset to be processed.
    tokenizer: The tokenizer to be used on the formatted dataset.
    max_ctx: The maximum context length for the tokenizer.

    Returns:
    ds: The processed and shuffled dataset ready for training.
    """

    def process_function(res):
        toks = tokenizer(res["txt"])
        return dict(
            input_ids=toks["input_ids"],
        )

    ds = raw_ds.map(process_function, batched=False).filter(lambda x: len(x["input_ids"]) < max_ctx)
    return ds


def hf_loader(*hf_name, split_names=None):
    if split_names is None:
        split_names = dict()
    return lambda split: hf_load_dataset(*hf_name, split=split_names.get(split, split))


##########
# ACTUAL DATASETS
##########


def format_amazon_polarity(ex, rng):
    return dict(txt=f"{ex['title']} {ex['content']}", hard_label=ex["label"])


register_dataset(
    "amazon_polarity",
    DatasetConfig(loader=hf_loader("amazon_polarity"), formatter=format_amazon_polarity),
)


def format_sciq(ex, rng):
    hard_label = int(rng.random() < 0.5)
    if hard_label:
        ans = ex["correct_answer"]
    else:
        ans = rng.choice([ex["distractor1"], ex["distractor2"], ex["distractor3"]])
    txt = f"Q: {ex['question']} A: {ans}"
    return dict(txt=txt, hard_label=hard_label)


register_dataset(
    "sciq",
    DatasetConfig(loader=hf_loader("sciq"), formatter=format_sciq),
)


def format_anthropic_hh(ex, rng):
    hard_label = int(rng.random() < 0.5)
    txt = ex["chosen"] if hard_label else ex["rejected"]
    return dict(txt=txt, hard_label=hard_label)


register_dataset(
    "anthropic_hh",
    DatasetConfig(loader=hf_loader("Anthropic/hh-rlhf"), formatter=format_anthropic_hh),
)


def format_cosmosqa(ex, rng):
    true_answer = ex["answer" + str(ex["label"])]
    if "None of the above choices ." in true_answer:
        hard_label = 0
    else:
        assert "None of the above choices" not in true_answer, true_answer
        hard_label = int(rng.random() < 0.5)
    if hard_label:
        answer = true_answer
    else:
        candidate_answers = [ex["answer" + str(i)] for i in range(4)]
        answer = rng.choice([x for x in candidate_answers if x != true_answer])
    txt = f"Context: {ex['context']}\nQuestion: {ex['question']}\nAnswer: {answer}"
    return dict(txt=txt, hard_label=hard_label)


register_dataset(
    "cosmos_qa",
    DatasetConfig(
        loader=hf_loader("cosmos_qa", split_names=dict(test="validation")),
        formatter=format_cosmosqa,
    ),
)


def format_boolq(ex, rng):
    hard_label = int(ex["answer"])
    txt = f"Passage: {ex['passage']}\nQuestion: {ex['question']}"
    return dict(txt=txt, hard_label=hard_label)

register_dataset(
    "boolq",
    DatasetConfig(
        loader=hf_loader("boolq", split_names=dict(test="validation")), formatter=format_boolq
    ),
)


def format_openbookQA(ex, rng):
    id = ex["id"]
    question_stem = ex["question_stem"]
    choices_text = ex['choices']['text']
    choices_labels = ex['choices']['label']
    correct_label = ex['answerKey']

    choices_formatted = ' '.join([f"{label}: {text}" for label, text in zip(choices_labels, choices_text)])
    
    correct_answer_index = choices_labels.index(correct_label)
    correct_answer_text = choices_text[correct_answer_index]
   
    txt = f"Question: {question_stem}\nChoices: {choices_formatted}\nCorrect Answer: {correct_answer_text}"
    return dict(txt=txt, hard_label=1)   # have to change how hard label is coded. 

register_dataset(
    "openbookqa",
    DatasetConfig(
        loader=hf_loader("allenai/openbookqa", "main", split_names=dict(test="validation")), formatter=format_openbookQA
    ),
)


def format_ethics_justice(ex, rng):
    txt = ex['text']
    hard_label = int(ex['label'])  # 1 or 0
    return dict(txt=txt, hard_label=hard_label)

register_dataset(
    "ethics_justice",
    DatasetConfig(
        loader=hf_loader("hendrycks/ethics", "justice"), formatter=format_ethics_justice
    ),
)


def format_paws(ex, rng):
    txt = f"Sentence 1: {ex['sentence1']} Sentence 2: {ex['sentence2']}"
    hard_label = int(ex['label'])
    return dict(txt=txt, hard_label=hard_label)

register_dataset(
    "paws_labeled_final",  # Unique name for the dataset registration.
    DatasetConfig(
        loader=hf_loader("paws", "labeled_final", split_names=dict(test="validation")), 
        formatter=format_paws
    ),
)


VALID_DATASETS: list[str] = list(_REGISTRY.keys())


"""
def format_mctaco(ex, rng):
    sentence = ex['sentence']
    question = ex['question']
    answer = ex['answer']
    label = int(ex['label'] == 'yes') # Convert 'yes'/'no' to binary label (1/0)
    txt = f"Context: {sentence}\nQuestion: {question}\nAnswer: {answer}"
    return dict(txt=txt, hard_label=label)

register_dataset(
    "mc_taco",
    DatasetConfig(
        loader=hf_loader("mc_taco", split_names=dict(test="validation")), formatter=format_mctaco
    ),
)

from datasets import disable_caching
disable_caching()

from weak_to_strong.datasets import load_dataset, VALID_DATASETS
import numpy as np

ds_name = "boolq"
print(VALID_DATASETS)

ds = load_dataset(ds_name, split_sizes=dict(train=500, test=10))
train = list(ds['train'])
test = list(ds['test'])
print(test[0])
print(np.mean([x['hard_label'] for x in train]))
"""
