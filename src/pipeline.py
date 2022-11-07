import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

import numpy as np
import pandas as pd

import os
from datetime import datetime
from tqdm import tqdm

from torch.utils.data import Dataset
from torch.utils.data import DataLoader

from torch.utils.tensorboard import SummaryWriter


class DatasetMapper(Dataset):
    """
    Dataset Mapper class to get image and label given an index.

    The class is written using torch parent class 'Dataset' for parallelizing and prefetching to accelerate training

    -----------
    Attributes:
    -----------
    - root_dir: str
        - directory path where images are stored

    - image_data_file: str
        - path to csv file that stores image names and labels

    - preprocessor: Instance of subclass of utils.Preprocessor
        - stores preprocessor with 'get' function that returns processed image given path to image and transformations

    - teacher_func: Function
        - Function to create label/feature-values given an image (for semi-supervised learning)
    """

    def __init__(self, root_dir, image_data_file, preprocessor, teacher_func: None):
        """
        Init for DatasetMapper

        -----
        Args:
        -----
        - root_dir: str
            - directory path where images are stored

        - image_data_file: str
            - path to csv file that stores image names and labels

        - preprocessor: Instance of subclass of utils.Preprocessor
            - stores preprocessor with 'get' function that returns processed image given path to image and transformations

        - teacher_func: Function
            - Function to create label/feature-values given an image (for semi-supervised learning)
        """

        self.root_dir = root_dir
        self.image_data_file = pd.read_csv(image_data_file)
        self.preprocessor = preprocessor
        self.teacher_func = teacher_func

    def __len__(self):
        """
        Function to get size of dataset
        """

        return self.image_data_file.shape[0]

    def __getitem__(self, idx):
        """
        Mapper function to get processed image and label given an index

        -----
        Args:
        -----
        - idx: int (python int / numpy.int / torch.int)
            - index of an image
            - idx >= 0 and idx < self.__len__()
        """

        # Convert to python int
        if torch.is_tensor(idx):
            idx = idx.tolist()

        # Get final image path from image data csv file
        img_name = os.path.join(self.root_dir, self.image_data_file.iloc[idx, 0])

        # Get processed image from preprocessor given image path
        image = self.preprocessor.get(img_name)

        # Get image label/feature-values from teacher function (if specified)
        if self.teacher_func is not None:
            img_label = self.teacher_func(image)
        else:
            # If not specified, get label/feature-values from image data csv file
            img_label = self.image_data_file.iloc[idx, 1]

        return image, img_label


class Pipeline:
    def __init__(
        self, name, model, batch_size, root_dir, image_data_file, preprocessor, **kwargs
    ):
        self.name = name + "_" + datetime.now().strftime("%Y%m%d-%H%M%S")

        self.model = model(**kwargs)
        self.batch_size = batch_size

        self.root_dir = root_dir
        self.image_data_file = image_data_file

        self.preprocessor = preprocessor

        self.args = kwargs

        # Set training device (CUDA-GPU / CPU)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print("Training Devide: {}".format(self.device))
        self.model.to(self.device)

        # Creating dataset mapper instances
        train_set = DatasetMapper(root_dir, image_data_file, self.preprocessor)
        test_set = DatasetMapper(root_dir, image_data_file, self.preprocessor)

        # Creating dataset loader to load data parallelly
        self.train_loader = DataLoader(
            train_set, batch_size=self.batch_size, num_workers=0, shuffle=True
        )
        self.test_loader = DataLoader(
            test_set, batch_size=self.batch_size, num_workers=0
        )

        # Create summary writers for tensorboard logs
        self.train_writer = SummaryWriter("logs/fit/" + self.name + "/train")
        self.valid_writer = SummaryWriter("logs/fit/" + self.name + "/validation")

    def train(self, **kwargs):
        assert "num_epochs" in kwargs.keys(), "Provide num_epochs in **kwargs"
        self.epochs = kwargs["num_epochs"]

        # Setting learning rate
        if "lr" in kwargs.keys():
            lr = kwargs["lr"]
        else:
            lr = 0.001

        # Setting optimzer
        if "optim" in kwargs.keys():
            optimizer = kwargs["optim"](self.model.parameters(), lr=lr)
        else:
            optimizer = optim.Adam(self.model.parameters(), lr=lr)

        # Learning rate scheduler for changing learning rate during training
        if "lr_scheduler" in kwargs.keys():
            lr_scheduler = kwargs["lr_scheduler"]
        elif "step_size_func" in kwargs.keys():
            step_size_func = kwargs["step_size_func"]
            lr_scheduler = optim.lr_scheduler.LambdaLR(optimizer, step_size_func)
        else:
            lr_scheduler = optim.lr_scheduler.LambdaLR(optimizer, lambda e: lr)

        # Loss function for minimizing loss
        assert (
            "loss_func" in kwargs.keys() and "loss_func_with_grad" in kwargs.keys()
        ), "loss_func and loss_func_with_grad must be present in **kwargs"
        loss_func_with_grad = kwargs["loss_func_with_grad"]
        loss_func = kwargs["loss_func"]

        # if 'loss_func' in kwargs.keys() and 'loss_func_with_grad' in kwargs.keys():
        # loss_func_with_grad = kwargs['loss_func_with_grad']
        # loss_func = kwargs['loss_func']
        # else:
        #     # cross entropy loss for learning features
        #     loss_func_with_grad = nn.CrossEntropyLoss()
        #     loss_func = F.cross_entropy
        #     # will require different loss for learning features
        #     # loss_func = nn.MSELoss()

        training_log = {"errors": [], "scores": []}
        validation_log = {"errors": [], "scores": []}

        # Training
        pbar = tqdm(range(self.epochs), desc="Training epoch")
        for epoch in pbar:
            pbar.set_postfix({"lr": lr_scheduler.get_lr()})

            ys = []
            y_preds = []

            # Putting model in training mode to calculate back gradients
            self.model.train()

            # Batch-wise optimization
            step = 0
            for x_train, y_train in self.train_loader:
                x = x_train.type(torch.LongTensor).to(self.device)
                y = y_train.type(torch.FloatTensor).to(self.device)

                # Forward pass
                y_pred = self.model(x)

                # Clearing previous epoch gradients
                optimizer.zero_grad()

                # Calculating loss
                loss = loss_func_with_grad(y_pred, y)

                # Backward pass to calculate gradients
                loss.backward()

                # Update gradients
                optimizer.step()

                # Save/show loss per step of training batches
                pbar.set_postfix({"training error": loss})
                training_log["errors"].append(
                    {"epoch": epoch, "step": step, "loss": loss}
                )

                self.train_writer.add_scalar("loss", loss)
                self.train_writer.flush()

                # Save y_true and y_pred in lists for calculating epoch-wise scores
                ys += list(y.cpu().detach().numpy())
                y_preds += list(y_pred.cpu().detach().numpy())

            # Update learning rate as defined above
            lr_scheduler.step()

            # Save/show training scores per epoch
            training_scores = []
            if "score_functions" in kwargs:
                for score_func in kwargs["score_functions"]:
                    score = score_func["func"](ys, y_preds)
                    training_scores.append({score_func["name"]: score})
                    self.train_writer.add_scalar(score_func["name"], score)

                self.train_writer.flush()
                training_log["scores"].append(
                    {"epoch": epoch, "scores": training_scores}
                )

            # Putting model in evaluation mode to stop calculating back gradients
            self.model.eval()
            with torch.no_grad():
                for x_test, y_test in self.test_loader:
                    x = x_test.type(torch.LongTensor).to(self.device)
                    y = y_test.type(torch.FloatTensor).to(self.device)

                    # Predicting
                    y_pred = self.model(x)

                    # Calculating loss
                    loss = loss_func(y_pred, y)

                    # Save/show loss per batch of validation data
                    # pbar.set_postfix({"test error": loss})
                    validation_log.append({"epoch": epoch, "loss": loss})
                    self.valid_writer.add_scalar("loss", loss)

                    # Save y_true and y_pred in lists for calculating epoch-wise scores
                    ys += list(y.cpu().detach().numpy())
                    y_preds += list(y_pred.cpu().detach().numpy())

            # Save/show validation scores per epoch
            validation_scores = []
            if "score_functions" in kwargs:
                for score_func in kwargs["score_functions"]:
                    score = score_func["func"](ys, y_preds)
                    validation_scores.append({score_func["name"]: score})
                    self.valid_writer.add_scalar(score_func["name"], score)

                self.valid_writer.flush()
                validation_log["scores"].append(
                    {"epoch": epoch, "scores": validation_scores}
                )

            # Saving model at specified checkpoints
            if "save_checkpoints" in kwargs.keys():
                if epoch % kwargs["save_checkpoints"]["epoch"] == 0:
                    torch.save(
                        {
                            "epoch": epoch,
                            "model_state_dict": self.model.state_dict(),
                            "optimizer_state_dict": optimizer.state_dict(),
                            "loss": loss,
                        },
                        kwargs["save_checkpoints"]["path"],
                    )

        return training_log, validation_log