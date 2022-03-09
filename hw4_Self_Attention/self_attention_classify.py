import os
import json
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from pathlib import Path
from tqdm import tqdm
from torch.optim import Optimizer
from torch.optim import AdamW
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data import Dataset, DataLoader, random_split
from torch.nn.utils.rnn import pad_sequence


class myDataset(Dataset):
    def __init__(self, data_dir, segment_len=128):
        self.data_dir = data_dir
        self.segment_len = segment_len

        mapping_path = Path(data_dir) / "mapping.json"  # path 的路径拼接符
        mapping = json.load(mapping_path.open())
        self.speaker2id = mapping["speaker2id"]  # 两层obj{"idxxxx":xxx,...}

        metadata_path = Path(data_dir) / "metadata.json"
        metadata = json.load(open(metadata_path))["speakers"]  # 取得 speakers 对象

        self.speaker_num = len(metadata.keys())  # keys返回json键值组成的字典即speaker的id
        self.data = []
        for speaker in metadata.keys():
            for utterances in metadata[speaker]:
                self.data.append([utterances["feature_path"], self.speaker2id[speaker]])

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        feat_path, speaker = self.data[index]
        mel = torch.load(os.path.join(self.data_dir, feat_path))
        """
        这里开始统一数据格式
        主要处理思路是 随机取起始位置截取segment_len大小的特征
        """
        if len(mel) > self.segment_len:
            start = random.randint(0, len(mel) - self.segment_len)  # 生成随机数，0 到 特征长度-定义的截取长度
            mel = torch.FloatTensor(mel[start:start + self.segment_len])
        else:
            mel = torch.FloatTensor(mel)
        speaker = torch.FloatTensor([speaker]).long()
        return mel, speaker

    def get_speaker_number(self):
        return self.speaker_num


"""
"n_mels": 40,
"speakers": {
     一个人的特征
        "id10473": [
                      {
                        "feature_path": "uttr-5c88b2f1803449789c36f14fb4d3c1eb.pt",
                        "mel_len": 652
                      },
                      {
                        "feature_path": "uttr-022a67baccc54bfda3567a7ac282a7b8.pt",
                        "mel_len": 564
                      },
                      ...
                      ...
                      ...
                      ],
                `````
                ````
"""


def collate_batch(batch):
    mel, speaker = zip(*batch)
    mel = pad_sequence(mel, batch_first=True, padding_value=-20)
    return mel, torch.FloatTensor(speaker).long()


def get_dataloader(data_dir, batch_size, n_workers):
    dataset = myDataset(data_dir)
    speaker_num = dataset.get_speaker_number()
    trainlen = int(0.9 * len(dataset))
    lengths = [trainlen, len(dataset) - trainlen]
    trainset, validset = random_split(dataset, lengths)

    train_loader = DataLoader(trainset, batch_size=batch_size, shuffle=True, drop_last=True, num_workers=n_workers,
                              pin_memory=True, collate_fn=collate_batch)
    valid_loader = DataLoader(validset, batch_size=batch_size, num_workers=n_workers, drop_last=True, pin_memory=True,
                              collate_fn=collate_batch)
    return train_loader, valid_loader, speaker_num


class Classifier(nn.Module):
    def __init__(self, d_model=80, n_spks=600, dropout=0.1):
        super().__init__()
        self.prenet = nn.Linear(40, d_model)
        # TODO:
        #   Change Transformer to Conformer.
        #   https://arxiv.org/abs/2005.08100
        self.encoder_layer = nn.TransformerEncoderLayer(d_model=d_model, dim_feedforward=256, nhead=2)

        self.pred_layer = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(),
            nn.Linear(d_model, n_spks),
        )

    def forward(self, mels):
        out = self.prenet(mels)
        out = out.permute(1, 0, 2)
        out = self.encoder_layer(out)
        out = out.transpose(0, 1)
        stats = out.mean(dim=1)

        out = self.pred_layer(stats)
        return out


def get_cosine_schedule_with_warmup(optimizer: Optimizer, num_warmup_steps: int, num_training_steps: int,
                                    num_cycles: float = 0.5, last_epoch: int = -1):
    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        progress = float(current_step - num_warmup_steps) / float(max(1, num_training_steps - num_warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def model_fn(batch, model, criterion, device):
    mels, labels = batch
    mels = mels.to(device)
    labels = labels.to(device)

    outs = model(mels)

    loss = criterion(outs, labels)

    preds = outs.argmax(1)
    accuracy = torch.mean((preds == labels).float())
    return loss, accuracy


def valid(dataloader, model, criterion, device):
    model.eval()
    running_loss = 0.0
    running_accuracy = 0.0
    pbar = tqdm(total=len(dataloader.dataset), ncols=0, desc="Valid", unit=" uttr")

    for i, batch in enumerate(dataloader):
        with torch.no_grad():
            loss, accuracy = model_fn(batch, model, criterion, device)
            running_loss += loss.item()
            running_accuracy += accuracy.item()
        pbar.update(dataloader.batch_size)
        pbar.set_postfix(loss=f"{running_loss / (i + 1):.2f}", accuracy=f"{running_accuracy / (i + 1):.2f}")
        pbar.close()

        model.train()
        return running_accuracy / len(dataloader)


def parse_args():
    config = {
        "data_dir": "../data/hw4/Dataset",
        "save_path": "model.ckpt",
        "batch_size": 32,
        "n_workers": 0,
        "valid_steps": 2000,
        "warmup_steps": 1000,
        "save_steps": 10000,
        "total_steps": 70000,
    }
    return config


def main(data_dir, save_path, batch_size, n_workers, valid_steps, warmup_steps, total_steps, save_steps):
    device = torch.device("cuda")

    train_loader, valid_loader, speaker_num = get_dataloader(data_dir, batch_size, n_workers)
    train_iterator = iter(train_loader)
    print(f"[Info]: 完成加载dataloader！", flush=True)

    model = Classifier(n_spks=speaker_num).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = AdamW(model.parameters(), lr=1e-3)
    scheduler = get_cosine_schedule_with_warmup(optimizer, warmup_steps, total_steps)
    print(f"[Info]: 完成实例化模型！", flush=True)

    best_accuracy = -1.0
    best_state_dict = None

    pbar = tqdm(total=valid_steps, ncols=0, desc="Train", unit=" step")

    for step in range(total_steps):
        try:
            batch = next(train_iterator)
        except StopIteration:
            train_iterator = iter(train_loader)
            batch = next(train_iterator)
        loss, accuracy = model_fn(batch, model, criterion, device)
        batch_loss = loss.item()
        batch_accuracy = accuracy.item()

        loss.backward()
        optimizer.step()
        scheduler.step()
        optimizer.zero_grad()

        pbar.update()
        pbar.set_postfix(loss=f"{batch_loss:.2f}", accuracy=f"{batch_accuracy:.2f}", step=step + 1)

        if (step + 1) % valid_steps == 0:
            pbar.close()

            valid_accuracy = valid(valid_loader, model, criterion, device)
            if valid_accuracy > best_accuracy:
                best_accuracy = valid_accuracy
                best_state_dict = model.state_dict()

            pbar = tqdm(total=valid_steps, ncols=0, desc="Train", unit=" step")

        if (step + 1) % save_steps == 0 and best_state_dict is not None:
            torch.save(best_state_dict, save_path)
            pbar.write(f"Step {step + 1}, best model saved. (accuracy={best_accuracy:.4f})")
    pbar.close()


if __name__ == '__main__':
    main(**parse_args())
