import argparse
import copy
import json
import os
import pickle
from string import Template
from time import sleep
import openai
import yaml

import pandas as pd
from openai import BadRequestError
from tqdm import tqdm


def load_client(key_path="openai_key.yaml"):
    openai._reset_client()
    key = yaml.safe_load(open(key_path))
    for k, v in key.items():
        setattr(openai, k, v)
    return openai._load_client()


def save_llm_results(llm_results, llm_output_path, name, keys=None):
    for key in llm_results if keys is None else keys:
        if key == "raw":
            pickle.dump(
                llm_results[key],
                open(
                    os.path.join(llm_output_path[key], f"{name}.pkl"),
                    "wb",
                ),
            )
        else:
            json.dump(
                llm_results[key],
                open(
                    os.path.join(llm_output_path[key], f"{name}.json"),
                    "w",
                ),
                indent=4,
            )


def load_llm_results(llm_output_path, name):
    llm_results = {k: [] for k in llm_output_path}

    for key in llm_output_path:
        if key == "raw" and os.path.exists(
            os.path.join(llm_output_path[key], f"{name}.pkl")
        ):
            llm_results[key] = pickle.load(
                open(
                    os.path.join(llm_output_path[key], f"{name}.pkl"),
                    "rb",
                )
            )
        elif os.path.exists(os.path.join(llm_output_path[key], f"{name}.json")):
            llm_results[key] = json.load(
                open(
                    os.path.join(llm_output_path[key], f"{name}.json"),
                    "r",
                )
            )

    return llm_results


def check_llm_results(llm_results):
    length = len(llm_results["input"])

    result_keys = ["raw", "processed", "back_input"]
    for key in result_keys:
        if len(llm_results[key]) < length:
            llm_results[key] = llm_results[key] + [None] * (
                length - len(llm_results[key])
            )

    inds = []
    for i in range(length):
        if (
            any([llm_results[key][i] is None for key in result_keys])
            or llm_results["back_input"][i]["messages"][-1]["content"]
            != llm_results["input"][i]["messages"][-1]["content"]
        ):
            inds.append(i)

    return inds


def get_llm_results(inputs, position=0):
    client = load_client("openai_key.yaml")
    for input in tqdm(inputs, desc="llm", position=position):
        try_num = 0
        while True:
            try:
                yield input, client.chat.completions.create(**input).to_dict()
                break
            except BadRequestError as e:
                if e.code == "context_length_exceeded":
                    if input["model"] == "gpt-3.5-turbo":
                        input["model"] = "gpt-3.5-turbo-16k"
                        continue
                    else:
                        print(f"context length exceeded, skip, error: {e}")
                        yield input, None
                else:
                    raise e
            except Exception as e:
                try_num += 1
                print(f"error: {e}, try num: {try_num}, retry after {try_num} min")
                sleep(try_num * 60)


def convert_table(name, data_path, output_path, chat_kwargs, position=0):
    llm_keys = ["linearization", "input", "raw", "processed", "back_input"]
    llm_output_path = {k: os.path.join(output_path, k) for k in llm_keys}

    for p in llm_output_path.values():
        os.makedirs(p, exist_ok=True)

    llm_results = load_llm_results(llm_output_path, name)
    data = pd.read_csv(os.path.join(data_path, f"{name}.csv"))
    data_len = len(list(data.iterrows()))
    # prepare linearization
    if len(llm_results["linearization"]) != data_len:
        prompt = chat_kwargs["messages"][-1]["content"]
        columns = [c for c in data if c in prompt]
        data = data[columns]

        linearization = [
            "; ".join([f"{name}: {value}" for name, value in zip(row.index, row)])
            for _, row in data.iterrows()
        ]
        llm_results["linearization"] = linearization
        save_llm_results(llm_results, llm_output_path, name, ["linearization"])

    # prepare llm inputs
    if len(llm_results["input"]) != data_len:
        for linear in llm_results["linearization"]:
            chat_kwargs["messages"][-1]["content"] = Template(prompt).safe_substitute(
                linearization=linear
            )
            llm_results["input"].append(copy.deepcopy(chat_kwargs))

        save_llm_results(llm_results, llm_output_path, name, ["input"])

    # get llm results
    inds = check_llm_results(llm_results)
    for ind, (input, result) in zip(
        inds, get_llm_results([llm_results["input"][ind] for ind in inds], position)
    ):
        llm_results["raw"][ind] = result
        if result is not None:
            llm_results["processed"][ind] = result["choices"][0]["message"]["content"]
        llm_results["back_input"][ind] = input

        save_llm_results(
            llm_results, llm_output_path, name, ["raw", "processed", "back_input"]
        )


def convert_ctod(data_path, output_path, chat_kwargs):
    for phase in tqdm(["I", "III", "II"], desc="phase", position=0):
        for split in tqdm(["valid", "train"], desc="split", position=1):
            convert_table(
                f"phase_{phase}_{split}",
                data_path,
                output_path,
                copy.deepcopy(chat_kwargs),
                2,
            )


def convert_hint(data_path, output_path, chat_kwargs):
    for phase in tqdm(["I", "II", "III"], desc="phase", position=0):
        for split in tqdm(["train", "valid", "test"], desc="split", position=1):
            convert_table(
                f"phase_{phase}_{split}",
                data_path,
                output_path,
                copy.deepcopy(chat_kwargs),
                2,
            )


def convert_ct_gov(data_path, output_path, chat_kwargs):
    convert_table(
        "data",
        data_path,
        output_path,
        chat_kwargs,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", nargs="+", default=None)
    args = parser.parse_args()
    return args


def main():
    datasets = {
        "ctod_description": {
            "func": convert_ctod,
            "data_path": "data/labeling",
            "output_path": "text_description",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please describe the sample using natural language.",
                    },
                ],
            },
        },
        "ctod_summary": {
            "func": convert_ctod,
            "data_path": "data/labeling",
            "output_path": "brief_summary",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please briefly summary the sample with its value in one sentence. You should describe the important values, like drugs and diseases, instead of just the name of columns in the table.\n"
                        + "A brief summary of other sample may look like:\n"
                        + "This study will test the ability of extended release nifedipine (Procardia XL), a blood pressure medication, to permit a decrease in the dose of glucocorticoid medication children take to treat congenital adrenal hyperplasia (CAH).\n"
                        + "Note that the example is not a summary of the sample above.\n",
                    },
                ],
            },
        },
        "hint": {
            "func": convert_hint,
            "data_path": "data/clinical-trial-outcome-prediction/data",
            "output_path": "text_description",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please describe the sample using natural language.",
                    },
                ],
            },
        },
        "hint_anypredict": {
            "func": convert_hint,
            "data_path": "data/clinical-trial-outcome-prediction/data",
            "output_path": "text_description_anypredict",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "smiless: list of SMILES of the drugs.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please describe the sample using natural language.",
                    },
                ],
            },
        },
        "hint_summary": {
            "func": convert_hint,
            "data_path": "data/clinical-trial-outcome-prediction/data",
            "output_path": "brief_summary",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please briefly summary the sample with its value in one sentence. You should describe the important values, like drugs and diseases, instead of just the name of columns in the table.\n"
                        + "A brief summary of other sample may look like:\n"
                        + "This study will test the ability of extended release nifedipine (Procardia XL), a blood pressure medication, to permit a decrease in the dose of glucocorticoid medication children take to treat congenital adrenal hyperplasia (CAH).\n"
                        + "Note that the example is not a summary of the sample above.\n",
                    },
                ],
            },
        },
        "hint_disease": {
            "func": convert_hint,
            "data_path": "data/clinical-trial-outcome-prediction/data",
            "output_path": "disease",
            "schema_definition": "diseases: list of disease names.\n",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please briefly summary the sample with its value in one sentence. Do not say anything about the table.",
                    },
                ],
            },
        },
        "ct_gov": {
            "func": convert_ct_gov,
            "data_path": "data/clinical_trials_gov",
            "output_path": "text_description",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "smiless: list of SMILES of the drugs.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please describe the sample using natural language.",
                    },
                ],
            },
        },
        "ct_gov_summary": {
            "func": convert_ct_gov,
            "data_path": "data/clinical_trials_gov",
            "output_path": "brief_summary",
            "schema_definition": "phase: the phase of the trial. phase I, or phase II, or phase III.\n"
            + "diseases: list of disease names.\n"
            + "icdcodes: list of icd-10 codes of diseases.\n"
            + "drugs: list of drug names.\n"
            + "smiless: list of SMILES of the drugs.\n"
            + "criteria: eligibility criteria.",
            "chat_kwargs": {
                "model": "gpt-3.5-turbo",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": "Here is the schema definition of the table:\n"
                        + "$schema_definition\n"
                        + "This is a sample from the table:\n"
                        + "$linearization\n"
                        + "Please briefly summary the sample in a few sentences.\n"
                        + "A brief summary of other sample may look like:\n"
                        + "This study will test the ability of extended release nifedipine (Procardia XL), a blood pressure medication, to permit a decrease in the dose of glucocorticoid medication children take to treat congenital adrenal hyperplasia (CAH).\n"
                        + "Note that the example is not a summary of the sample above.\n",
                    },
                ],
            },
        },
    }

    args = parse_args()
    if args.tasks is None:
        args.tasks = datasets.keys()

    for dataset_name in args.tasks:
        dataset = datasets[dataset_name]
        chat_kwargs = dataset["chat_kwargs"]
        chat_kwargs["messages"][-1]["content"] = Template(
            chat_kwargs["messages"][-1]["content"]
        ).safe_substitute(schema_definition=dataset["schema_definition"])
        output_path = dataset.get("output_path", "cache")
        output_path = os.path.join(dataset["data_path"], output_path)
        print(f"convert {dataset_name}")
        dataset["func"](dataset["data_path"], output_path, chat_kwargs)


if __name__ == "__main__":
    main()
