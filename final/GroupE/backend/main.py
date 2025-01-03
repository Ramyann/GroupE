from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.model_selection import train_test_split, KFold, LeaveOneOut
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import KNeighborsClassifier
from sklearn.naive_bayes import GaussianNB
from sklearn.svm import SVC
from sklearn.metrics import classification_report, accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
import torch
import torch.nn as nn
import pandas as pd
import numpy as np
import joblib
import uvicorn
from typing import List
from joblib import Parallel, delayed

class TrainRequest(BaseModel):
    validation_method: str

class PredictRequest(BaseModel):
    data_row: List[float]
    model_type: str

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Neural Network for Diabetes prediction
class DiabetesNN(nn.Module):
    def __init__(self, input_size):
        super(DiabetesNN, self).__init__()
        self.fc1 = nn.Linear(input_size, 16)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(16, 8)
        self.fc3 = nn.Linear(8, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        x = self.relu(self.fc1(x))
        x = self.relu(self.fc2(x))
        x = self.sigmoid(self.fc3(x))
        return x

data = None
scaler = None
trained_models = {}

def load_data():
    global data
    try:
        data = pd.read_csv('./diabetes.csv')
        return True
    except Exception as e:
        print(f"Error loading data: {e}")
        return False

def preprocess_data():
    global scaler

    if data is None:
        raise HTTPException(status_code=500, detail="Data not loaded")

    # Handle missing values (0 values in certain columns)
    columns_to_process = ['Glucose', 'BloodPressure', 'SkinThickness', 'Insulin', 'BMI']
    processed_data = data.copy()

    for column in columns_to_process:
        processed_data[column] = processed_data[column].replace(0, np.nan)
        processed_data[column] = processed_data[column].fillna(processed_data[column].mean())

    X = processed_data.iloc[:, :-1].values  # Convert to numpy array
    y = processed_data.iloc[:, -1].values  # Convert to numpy array

    # Scale the features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    return X_scaled, y

def save_model(model, model_name):
    with open(f"{model_name}.pkl", "wb") as f:
        joblib.dump(model, f)

def load_model(model_name):
    try:
        with open(f"{model_name}.pkl", "rb") as f:
            return joblib.load(f)
    except FileNotFoundError:
        return None
    
    
    
def evaluate_model(model, X_test, y_test):
    predictions = model.predict(X_test)
    probas = model.predict_proba(X_test) if hasattr(model, "predict_proba") else None

    accuracy = accuracy_score(y_test, predictions)
    precision = precision_score(y_test, predictions, zero_division=0)
    recall = recall_score(y_test, predictions, zero_division=0)
    f1 = f1_score(y_test, predictions, zero_division=0)
    roc_auc = roc_auc_score(y_test, probas[:, 1]) if probas is not None else None

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1_score": f1,
        "roc_auc": roc_auc,
    }

def evaluate_fold(model, X_train, X_test, y_train, y_test):
    model.fit(X_train, y_train)
    return evaluate_model(model, X_test, y_test)



# API Endpoints
@app.on_event("startup")
async def startup_event():
    if not load_data():
        raise HTTPException(status_code=500, detail="Failed to load initial data")

@app.get("/startup")
async def startup_status():
    if data is not None:
        return {"status": "Model loaded successfully"}
    else:
        raise HTTPException(status_code=500, detail="Model not loaded")

# API Endpoint to train models

@app.post("/train")
async def train_endpoint(request: TrainRequest):
    try:
        X, y = preprocess_data()
        validation_method = request.validation_method

        if validation_method == "holdout":
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            results = {}

            models = {
                "kNN": KNeighborsClassifier(n_neighbors=5),
                "Bayesian": GaussianNB(),
                "SVM": SVC(probability=True)
            }

            for name, model in models.items():
                model.fit(X_train, y_train)
                results[name] = evaluate_model(model, X_test, y_test)
                save_model(model, name)

            return results

        elif validation_method in ["3-fold", "10-fold"]:
            n_splits = 3 if validation_method == "3-fold" else 10
            kf = KFold(n_splits=n_splits)
            results = {}

            models = {
                "kNN": KNeighborsClassifier(n_neighbors=5),
                "Bayesian": GaussianNB(),
                "SVM": SVC(probability=True)
            }

            for name, model in models.items():
                metrics_sum = {
                    "accuracy": 0,
                    "precision": 0,
                    "recall": 0,
                    "f1_score": 0,
                    "roc_auc": 0
                }
                valid_folds = 0

                for train_index, test_index in kf.split(X):
                    X_train_cv, X_test_cv = X[train_index], X[test_index]
                    y_train_cv, y_test_cv = y[train_index], y[test_index]

                    model.fit(X_train_cv, y_train_cv)
                    fold_metrics = evaluate_model(model, X_test_cv, y_test_cv)

                    for key in metrics_sum:
                        if fold_metrics[key] is not None:
                            metrics_sum[key] += fold_metrics[key]
                    valid_folds += 1

                results[name] = {key: metrics_sum[key] / valid_folds for key in metrics_sum}
                save_model(model, name)

            return results

        elif validation_method == "leave-one-out":
            if len(X) > 1000:
                raise HTTPException(status_code=400, detail="LOO is computationally expensive for large datasets")

            loo = LeaveOneOut()
            results = {}

            models = {
                "kNN": KNeighborsClassifier(n_neighbors=5),
                "Bayesian": GaussianNB(),
                "SVM": SVC(probability=True)
            }

            for name, model in models.items():
                fold_results = Parallel(n_jobs=-1)(delayed(evaluate_fold)(
                    model, X[train_index], X[test_index], y[train_index], y[test_index]
                ) for train_index, test_index in loo.split(X))

                metrics_sum = {key: 0 for key in fold_results[0]}
                for fold_metrics in fold_results:
                    for key in metrics_sum:
                        metrics_sum[key] += fold_metrics[key]

                results[name] = {key: metrics_sum[key] / len(fold_results) for key in metrics_sum}
                save_model(model, name)

            return results

        else:
            raise HTTPException(status_code=400, detail="Invalid validation method")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/predict")
async def predict_endpoint(request: PredictRequest):
    try:
        model_name = request.model_type
        model = load_model(model_name)

        if model is None:
            # Train the model if it's not trained yet
            X, y = preprocess_data()
            results = train_model(X, y, "holdout")  # Default to holdout for initial training
            model = load_model(model_name)  # Reload the model after training
            if model is None:
                raise HTTPException(status_code=500, detail="Model failed to load after training")

        data_row_scaled = scaler.transform([request.data_row])  # Scale the incoming data row
        model_type = model_name.lower()

        if model_type == "neural network":
            with torch.no_grad():
                model.eval()
                data_tensor = torch.tensor(data_row_scaled, dtype=torch.float32)
                prediction = model(data_tensor).item()
            return {"prediction": prediction}
        else:
            prediction = model.predict(data_row_scaled)[0]
            return {"prediction": prediction}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Run FastAPI app
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)



# sklearn: A library for machine learning that includes algorithms like k-NN, Naive Bayes, and SVM, as well as utility functions for training and evaluation.
# torch: PyTorch is used to implement a neural network for diabetes prediction.

# Global Variables
# data: Stores the diabetes dataset (loaded from a CSV file).
# scaler: Stores the StandardScaler used to scale the features of the dataset.
# trained_models: A dictionary that stores the trained models for later use in predictions.

# /predict: This endpoint is used for making predictions with a trained model. It takes data_row (the features for prediction) and model_type (the model to use for prediction). The model is loaded, the data is scaled, and a prediction is made.
# For the Neural Network, predictions are made by passing the data through the network after converting it to a tensor.
# For other models, predictions are made using predict() and the probability of each class is returned.

# Model Training:
# Three models are trained using sklearn: k-NN, Naive Bayes, and SVM.
# A neural network is trained using PyTorch.


# Holdout Method:
# This is the simplest validation method. The dataset is split into two
# parts: training and testing. By default, 80% of the data is used for training and 20% for testing (train_test_split function)


# What is the role of the neural network in this application?
# The neural network (implemented in the DiabetesNN class) is used to predict the likelihood of a patient having diabetes based on input features such as Glucose, Blood Pressure, BMI, etc. The role of the neural network is:
# Architecture: The network has three fully connected layers (fc1, fc2, fc3)
# with ReLU activation functions between them. The final layer uses the Sigmoid activation function to produce a value between 0 and 1, which is interpreted as the probability of a positive diagnosis (class 1 for diabetes).
# Training: The neural network is trained using backpropagation with a
# loss function (BCELoss), which is suitable for binary classification tasks 
# (since diabetes prediction is a binary classification problem). The optimizer used is Adam, which adjusts the weights to minimize the loss.
# Purpose: It offers an alternative to traditional machine learning algorithms 
# (like k-NN, Naive Bayes, or SVM) by capturing more complex relationships in
# the data. While simpler models can be more interpretable, the neural network may capture non-linearities better and provide improved performance on larger, more complex datasets.


# Why is it necessary to use a scaler like StandardScaler?
# Scaling is crucial for several reasons:
# Improves Performance: Many machine learning algorithms (especially
# those based on distance metrics, like k-NN) perform better when the features
# are on the same scale. If the features have different ranges, the algorithm may 
# be biased towards features with larger values.
# Speeds Up Convergence: For optimization-based models like SVM or
# Neural Networks, scaling can help the model converge faster because 
# the gradients for all features are on a similar scale, preventing the optimizer from taking erratic steps.
# Ensures Model Compatibility: Some algorithms assume the data is 
# standardized (e.g., SVM or logistic regression), so it's essential
# to scale the features before feeding them into such models.


# Evaluation Metrics:

# Accuracy = (True Positives + True Negatives) / Total Instances
# Definition: Accuracy is the proportion of correctly classified instances (both true positives and true negatives) out of all the instances in the dataset.
# it can be misleading when the dataset is imbalanced

# Precision = True Positives / ( True Positives + False Positives )
# Definition: Precision is the proportion of true positive predictions (correctly predicted instances of the positive class) out of all the instances that the model predicted as positive.
# It answers the question: Of all the positive predictions, how many were actually positive?

# Recall (Sensitivity or True Positive Rate)
# Definition: Recall is the proportion of true positive predictions (correctly predicted instances of the positive class) out of all the instances that are actually positive in the dataset.
# Recall = True Positives / ( True Positives + False Negatives )
# It answers the question: Of all the actual positive instances, how many did the model correctly identify?

# F1-Score Definition: The F1-Score is the harmonic mean of Precision and Recall. It is a balanced metric that takes both false positives and false negatives into account, especially useful when the dataset is imbalanced.
# F1-Score = 2 * [ (precision * recall) / ( Precision + recall )]
# The F1-Score ranges from 0 to 1, where 1 is the best possible value (perfect precision and recall) and 0 is the worst.

# ROC-AUC (Receiver Operating Characteristic - Area Under the Curve)
# Definition: ROC-AUC measures the ability of a model to distinguish between positive and negative classes. It calculates the area under the ROC curve, where:
# True Positive Rate (Recall) is plotted on the y-axis.
# False Positive Rate (1 - Specificity) is plotted on the x-axis.



