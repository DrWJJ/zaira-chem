import os
import numpy as np
import uuid
import shutil
import joblib
import json
from sklearn.model_selection import KFold

from flaml import AutoML
from ..tools.ghost.ghost import GhostLight

from ..vars import N_FOLDS


DEFAULT_TIME_BUDGET_MIN = 0.1


class FlamlSettings(object):
    def __init__(self, y):
        # TODO: This will help elucidate best metric, or use multitasking if necessary
        self.y = np.array(y)

    def cast_groups(self, groups):
        if groups is not None:
            return groups
        else:
            splitter = KFold(n_splits=N_FOLDS, shuffle=True)
            groups = np.zeros(len(self.y), dtype=int)
            i = 0
            for _, test_idx in splitter.split(folds):
                groups[test_idx] = i
                i += 1
            return list(groups)

    def get_metric(self, task):
        # TODO: Adapt to imbalanced learning
        if task == "classification":
            return "auto"
        else:
            return "auto"

    def get_automl_settings(self, task, time_budget, estimators, groups):
        automl_settings = {
            "time_budget": int(time_budget * 60) + 1,  #  in seconds
            "metric": self.get_metric(task),
            "task": task,
            "log_file_name": "automl.log",
            "log_training_metric": True,
            "verbose": 3,
        }
        if estimators is not None:
            automl_settings["estimator_list"] = estimators
        groups = self.cast_groups(groups)
        automl_settings["split_type"] = "group"
        automl_settings["groups"] = groups
        return automl_settings


class FlamlEstimator(object):
    def __init__(self, task, name=None):
        if name is None:
            name = str(uuid.uuid4())
        self.name = name
        self.task = task
        self.main_mdl = None
        if self.task == "classification":
            self.is_clf = True
        else:
            self.is_clf = False

    def _clean_log(self, automl_settings):
        log_file = automl_settings["log_file_name"]
        # remove catboost info folder if generated
        cwd = os.getcwd()
        catboost_info = os.path.join(cwd, "catboost_info")
        if os.path.exists(catboost_info):
            shutil.rmtree(catboost_info)
        if os.path.exists(log_file):
            os.remove(log_file)

    def fit_main(self, X, y, time_budget, estimators, groups):
        automl_settings = FlamlSettings(y).get_automl_settings(
            task=self.task,
            time_budget=time_budget,
            estimators=estimators,
            groups=groups,
        )
        self.main_groups = groups
        self.main_mdl = AutoML()
        self.main_mdl.fit(X_train=X, y_train=y, **automl_settings)
        self.main_automl_settings = automl_settings

    def fit_predict_by_group(self, X, y):
        assert self.main_mdl is not None
        automl_settings = self.main_automl_settings
        y = np.array(y)
        y_hat = np.zeros(y.shape)
        groups = np.array(automl_settings["groups"])
        automl_settings["time_budget"] = 10  # TODO
        folds = sorted(set(groups))
        best_models = self.main_mdl.best_config_per_estimator
        best_estimator = self.main_mdl.best_estimator
        starting_point = {best_estimator: best_models[best_estimator]}
        for fold in folds:
            tag = "fold_{0}".format(fold)
            tr_idxs = []
            te_idxs = []
            for i, g in enumerate(groups):
                if g != fold:
                    tr_idxs += [i]
                else:
                    te_idxs += [i]
            tr_idxs = np.array(tr_idxs)
            te_idxs = np.array(te_idxs)
            X_tr = X[tr_idxs]
            X_te = X[te_idxs]
            y_tr = y[tr_idxs]
            y_te = y[te_idxs]
            groups_tr = groups[tr_idxs]
            unique_groups = list(set(groups_tr))
            groups_mapping = {}
            for i, g in enumerate(unique_groups):
                groups_mapping[g] = i
            automl_settings["groups"] = np.array([groups_mapping[g] for g in groups_tr])
            automl_settings["n_splits"] = len(unique_groups)
            automl_settings["estimator_list"] = [best_estimator]
            model = AutoML()
            model.fit(
                X_train=X_tr,
                y_train=y_tr,
                starting_points=starting_point,
                **automl_settings
            )
            if self.is_clf:
                y_te_hat = model.predict_proba(X_te)[:, 1]
            else:
                y_te_hat = model.predict(X_te)
            self._clean_log(automl_settings)
            y_hat[te_idxs] = y_te_hat
        return y_hat

    def fit_predict(self, X, y, time_budget, estimators, groups):
        self.fit_main(X, y, time_budget, estimators, groups)
        y_hat = self.fit_predict_by_group(X, y)
        self._clean_log(self.main_automl_settings)
        return y_hat


class FlamlClassifier(object):
    def __init__(self, name=None):
        self.task = "classification"
        self.estimator = FlamlEstimator(task=self.task, name=name)
        self.name = self.estimator.name

    def fit(
        self, X, y, time_budget=DEFAULT_TIME_BUDGET_MIN, estimators=None, groups=None
    ):
        y_hat = self.estimator.fit_predict(X, y, time_budget, estimators, groups)
        threshold = GhostLight().get_threshold(y, y_hat)
        self.y_hat = y_hat
        self.y = y
        self.threshold = threshold

    def save(self, file_name):
        joblib.dump(self.estimator.main_mdl, file_name)
        data = {"threshold": self.threshold}
        with open(file_name.split(".")[0] + ".json", "w") as f:
            json.dump(data, f)

    def load(self, file_name):
        model = joblib.load(file_name)
        with open(file_name.split(".")[0] + ".json", "r") as f:
            data = json.load(f)
        threshold = data["threshold"]
        return FlamlClassifierArtifact(model, threshold)


class FlamlClassifierArtifact(object):
    def __init__(self, model, threshold):
        self.model = model
        self.threshold = threshold

    def predict_proba(self, X):
        return self.model.predict_proba(X)[:, 1]

    def predict(self, X):
        y_hat = self.predict_proba(X)
        y_bin = []
        for y in y_hat:
            if y > self.threshold:
                y_bin += [1]
            else:
                y_bin += [0]
        return np.array(y_bin, dtype=np.uint8)


class FlamlRegressor(object):
    def __init__(self, name=None):
        self.task = "regression"
        self.estimator = FlamlEstimator(task=self.task, name=name)
        self.name = self.estimator.name

    def fit(
        self, X, y, time_budget=DEFAULT_TIME_BUDGET_MIN, estimators=None, groups=None
    ):
        y_hat = self.estimator.fit_predict(X, y, time_budget, estimators, groups)
        self.y_hat = y_hat
        self.y = y

    def save(self, file_name):
        joblib.dump(self.estimator.main_mdl, file_name)
        data = {}
        with open(file_name.split(".")[0] + ".json", "w") as f:
            json.dump(data, f)

    def load(self, file_name):
        model = joblib.load(file_name)
        with open(file_name.split(".")[0] + ".json", "r") as f:
            data = json.load(f)
        return FlamlRegressorArtifact(model)


class FlamlRegressorArtifact(object):
    def __init__(self, model):
        self.model = model

    def predict(self, X):
        return self.model.predict(X)
