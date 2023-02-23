import torch
import wandb
import pprint

from torch import nn
from torch.optim.lr_scheduler import MultiStepLR

from generator.meld import vectorize_log, prepare_log
from generator.data_loader import get_loader
from generator.models import MTCondLSTM
from generator.training import train
from generator.utils import get_runs, read_data


def get_args_parser(add_help=True):
    import argparse

    parser = argparse.ArgumentParser(
        description="Pytorch Implementation for Condition-based Trace Generator",
        add_help=add_help,
    )

    parser.add_argument(
        "--dataset",
        default="RequestForPayment",
        type=str,
        help="dataset",
    )
    parser.add_argument(
        "--condition",
        default="trace_time",
        type=str,
        help="condition",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        type=str,
        help="device (Use cuda or cpu Default: cuda)",
    )

    return parser


def get_vocabs(log, features=["activity", "resource"]):
    vocabs = dict()
    for f in features:
        stoi = {v: k for k, v in enumerate(log.loc[:, f].unique())}
        vocabs[f] = {
            "stoi": stoi,
            "size": len(stoi),
            "emb_dim": int(len(stoi) ** (1 / 2))
            if int(len(stoi) ** (1 / 2)) > 2
            else 2,
        }
    return vocabs


def main(config=None):
    params = get_args_parser().parse_args()
    if params.device == "cuda":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # print("Warning; gpu not available") #todo

    logger = wandb.init(config=config)
    logger.config.update(
        {
            "dataset": params.dataset,
            "condition": params.condition,
            "device": device.type,
        }
    )
    config = logger.config
    log = read_data(f"data/{params.dataset}/log.csv")
    log["target"] = log[params.condition]
    log.drop(["trace_time", "resource_usage"], axis=1, inplace=True)
    log = prepare_log(log)
    vocabs = get_vocabs(log)
    # encoding
    for f in vocabs:
        log.loc[:, f] = log.loc[:, f].transform(lambda x: vocabs[f]["stoi"][x])

    # ToDo how to track cat features? here we have (act, res, rt)
    data_train, data_test = vectorize_log(log)

    train_loader = get_loader(data_train, batch_size=config.batch_size)
    test_loader = get_loader(data_test, batch_size=1024, shuffle=False)

    torch.manual_seed(0)
    model = MTCondLSTM(vocabs=vocabs, batch_size=config.batch_size)

    def init_weights(m):
        if isinstance(m, nn.Linear):
            torch.nn.init.kaiming_normal_(m.weight, nonlinearity="relu")
            m.bias.data.fill_(0.5)

    model.apply(init_weights)

    model.to(device)
    wandb.watch(model, log="all")
    # X, y = next(iter(train_loader))
    # model(X)

    criterion = {"clf": nn.CrossEntropyLoss(), "reg": nn.MSELoss()}
    if config.optimizer == "sgd":
        optm = torch.optim.SGD(
            model.parameters(),
            lr=config.lr,
            momentum=0.9,
            weight_decay=config.weight_decay,
        )
    elif config.optimizer == "adam":
        optm = torch.optim.Adam(
            model.parameters(), lr=config.lr, weight_decay=config.weight_decay
        )
    sc = MultiStepLR(optm, milestones=[25, 35], gamma=0.1)

    train(
        model=model,
        train_loader=train_loader,
        test_loader=test_loader,
        loss_fn=criterion,
        optimizer=optm,
        sc=sc,
        logger=logger,
    )
    logger.finish()

if __name__ == "__main__":
    params = get_args_parser().parse_args()
    sweep_config = {
        "method": "bayes",
        "name": params.dataset,
        "metric": {"name": "test_loss", "goal": "minimize"},
        "parameters": {
            "optimizer": {"values": ["adam", "sgd"]},
            "lr": {"max": 1e-3, "min": 1e-6},
            "epochs": {"values": [50]},
            "batch_size": {"values": [64, 256, 512]},
            "weight_decay": {"values": [0.0, 1e-2, 1e-3]},
        },
    }
    sweep_id = wandb.sweep(sweep_config, project=f"multi-task")
    wandb.agent(sweep_id, main, count=5)