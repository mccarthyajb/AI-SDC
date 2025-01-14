# safekaeras.py:
# UWE 2022

# general imports
import copy
import os
import sys
from typing import Any

import numpy as np

# tensorflow imports
import tensorflow as tf
import tensorflow_privacy as tf_privacy
from dictdiffer import diff
from tensorflow.keras import Model as KerasModel
from tensorflow_privacy import DPModel
from tensorflow_privacy.privacy.analysis import compute_dp_sgd_privacy
from tensorflow_privacy.privacy.optimizers import dp_optimizer_keras

# safemodel superclass
from safemodel.safemodel import SafeModel


def same_configs(m1: Any, m2: Any) -> tuple[bool, str]:
    if len(m1.layers) != len(m2.layers):
        return False, "different numbers of layers"
    for layer in range(len(m1.layers)):
        m1_layer_config = m1.layers[layer].get_config()
        m2_layer_config = m2.layers[layer].get_config()
        match = list(diff(m1_layer_config, m2_layer_config, expand=True))
        if len(match) > 0:
            disclosive = True
            msg = f"Layer {layer} configs differ in {len(match)} places:\n"
            for i in range(len(match)):
                if match[i][0] == "change":
                    msg += f"parameter {match[i][1]} changed from {match[i][2][1]} "
                    msg += f"to {match[i][2][0]} after model was fitted.\n"
                else:
                    msg += f"{match[i]}"
            return False, msg

    return True, "configurations match"


def same_weights(m1: Any, m2: Any) -> tuple[bool, str]:
    if len(m1.layers) != len(m2.layers):
        return False, "different numbers of layers"
    numlayers = len(m1.layers)
    for layer in range(numlayers):
        m1layer = m1.layers[layer].get_weights()
        m2layer = m2.layers[layer].get_weights()
        if len(m1layer) != len(m2layer):
            return False, f"layer {layer} not the same size."
        for dim in range(len(m1layer)):
            m1d = m2layer[dim]
            m2d = m2layer[dim]
            # print(type(m1d), m1d.shape)
            if not np.array_equal(m1d, m2d):
                return False, f"dimension {dim} of layer {layer} differs"
    return True, "weights match"


def test_checkpoint_equality(v1: str, v2: str) -> tuple[bool, str]:
    """compares two checkpoints saved with tensorflow save_model
    On the assumption that the optimiser is not going to be saved,
    and that the model is going to be saved in frozen form
    this only checks the architecture and weights layer by layer
    """
    msg = ""
    same = True

    try:
        model1 = tf.keras.models.load_model(v1)
    except Exception as e:
        msg = f"Error re-loading  model from {v1}:  {e}"
        return False, msg
    try:
        model2 = tf.keras.models.load_model(v2)
    except Exception as e:
        msg = f"Error re-loading  model from {v2}: {e}"
        return False, msg

    same_config, config_message = same_configs(model1, model2)
    if not same_config:
        msg += config_message
    same_weight, weights_message = same_weights(model1, model2)
    if not same_weight:
        msg += weights_message

    return same, msg


class Safe_KerasModel(KerasModel, SafeModel):
    """Privacy Protected Wrapper around  tf.keras.Model class from tensorflow 2.8"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Creates model and applies constraints to params"""

        the_args = args
        the_kwargs = kwargs

        # initialise all the values that get provided as options to keras
        # and also l2 norm clipping and learning rates, batch sizes
        if "inputs" in kwargs.keys():
            inputs = the_kwargs["inputs"]
        elif len(args) == 3:  # defaults is for Model(input,outputs,names)
            inputs = args[0]

        self.outputs = None
        if "outputs" in kwargs.keys():
            outputs = the_kwargs["outputs"]
        elif len(args) == 3:
            # self.outputs = args[1]
            outputs = args[1]

        super().__init__(inputs=inputs, outputs=outputs)

        # set values where the user has supplied them
        # if not supplied set to a value that preliminary_check
        # will over-ride with TRE-specific values from rules.json

        defaults = {
            "l2_norm_clip": 1.0,
            "noise_multiplier": 0.5,
            "min_epsilon": 10,
            "delta": 1e-5,
            "batch_size": 25,
            "num_microbatches": None,
            "learning_rate": 0.1,
            "optimizer": tf_privacy.DPKerasSGDOptimizer,
            "num_samples": 250,
            "epochs": 20,
        }

        for key in defaults.keys():
            if key in kwargs.keys():
                setattr(self, key, kwargs[key])
            else:
                setattr(self, key, defaults[key])

        if self.batch_size == 0:
            print("batch_size should not be 0 - division by zero")

        SafeModel.__init__(self)

        self.model_type: str = "KerasModel"
        # remove. this from default class
        _ = self.__dict__.pop("saved_model")
        super().preliminary_check(apply_constraints=True, verbose=True)

    def dp_epsilon_met(
        self, num_examples: int, batch_size: int = 0, epochs: int = 0
    ) -> tuple[bool, str]:
        """Checks if epsilon is sufficient for Differential Privacy
        Provides feedback to user if epsilon is not sufficient"""
        privacy = compute_dp_sgd_privacy.compute_dp_sgd_privacy(
            n=num_examples,
            batch_size=batch_size,
            noise_multiplier=self.noise_multiplier,
            epochs=epochs,
            delta=self.delta,
        )
        if privacy[0] < self.min_epsilon:
            ok = True
        else:
            ok = False
        return ok, privacy[0]

    def check_epsilon(
        self, num_samples: int, batch_size: int, epochs: int
    ) -> tuple[bool, str]:
        """Computes the level of privacy guarantee is within recommended limits,
        and produces feedback"
        """
        msg = ""
        if batch_size == 0:
            print("Division by zero setting batch_size =1")
            batch_size = 1

        ok, self.current_epsilon = self.dp_epsilon_met(
            num_examples=num_samples, batch_size=batch_size, epochs=epochs
        )

        if ok:
            msg = (
                "The requirements for DP are met, "
                f"current epsilon is: {self.current_epsilon}."
                "Calculated from the parameters:  "
                f"Num Samples = {num_samples}, "
                f"batch_size = {batch_size}, epochs = {epochs}.\n"
            )
        if not ok:
            msg = (
                f"The requirements for DP are not met, "
                f"current epsilon is: {self.current_epsilon}.\n"
                f"To attain recommended DP the following parameters can be changed:  "
                f"Num Samples = {num_samples},"
                f"batch_size = {batch_size},"
                f"epochs = {epochs}.\n"
            )
        print(msg)
        return ok, msg

    def check_optimizer_is_DP(self, optimizer) -> tuple[bool, str]:
        DPused = False
        reason = "None"
        if "_was_dp_gradients_called" not in optimizer.__dict__:
            reason = (
                "optimizer does not contain key _was_dp_gradients_called"
                " so is not DP."
            )
            DPused = False
        else:
            reason = (
                "optimizer does  contain key _was_dp_gradients_called"
                " so should be DP."
            )
            DPused = True
        return DPused, reason

    def check_DP_used(self, optimizer) -> tuple[bool, str]:
        DPused = False
        reason = "None"
        if "_was_dp_gradients_called" not in optimizer.__dict__:
            reason = (
                "optimizer does not contain key _was_dp_gradients_called "
                "so is not DP."
            )
            DPused = False
        elif optimizer._was_dp_gradients_called == False:
            reason = "optimizer has been changed but fit() has not been rerun."
            DPused = False
        elif optimizer._was_dp_gradients_called == True:
            reason = (
                " value of optimizer._was_dp_gradients_called is True, "
                "so DP variant of optimizer has been run"
            )
            DPused = True
        else:
            reason = "unrecognised combination"
            DPused = False

        return DPused, reason

    def check_optimizer_allowed(self, optimizer) -> tuple[bool, str]:
        disclosive = True
        reason = "None"
        allowed_optimizers = [
            "tensorflow_privacy.DPKerasAdagradOptimizer",
            "tensorflow_privacy.DPKerasAdamOptimizer",
            "tensorflow_privacy.DPKerasSGDOptimizer",
        ]
        print(f"{str(self.optimizer)}")
        if self.optimizer in allowed_optimizers:
            disclosive = False
            reason = f"optimizer {self.optimizer} allowed"
        else:
            disclosive = True
            reason = f"optimizer {self.optimizer} is not allowed"

        return reason, disclosive

    def compile(
        self, optimizer=None, loss="categorical_crossentropy", metrics=["accuracy"]
    ):

        replace_message = "WARNING: model parameters may present a disclosure risk"
        using_DP_SGD = "Changed parameter optimizer = 'DPKerasSGDOptimizer'"
        Using_DP_Adagrad = "Changed parameter optimizer = 'DPKerasAdagradOptimizer'"
        using_DP_Adam = "Changed parameter optimizer = 'DPKerasAdamOptimizer'"

        optimizer_dict = {
            None: (using_DP_SGD, tf_privacy.DPKerasSGDOptimizer),
            tf_privacy.DPKerasSGDOptimizer: ("", tf_privacy.DPKerasSGDOptimizer),
            tf_privacy.DPKerasAdagradOptimizer: (
                "",
                tf_privacy.DPKerasAdagradOptimizer,
            ),
            tf_privacy.DPKerasAdamOptimizer: ("", tf_privacy.DPKerasAdamOptimizer),
            "Adagrad": (
                replace_message + Using_DP_Adagrad,
                tf_privacy.DPKerasAdagradOptimizer,
            ),
            "Adam": (replace_message + using_DP_Adam, tf_privacy.DPKerasAdamOptimizer),
            "SGD": (replace_message + using_DP_SGD, tf_privacy.DPKerasSGDOptimizer),
        }

        val = optimizer_dict.get(self.optimizer, "unknown")
        if val == "unknown":
            opt_msg = using_DP_SGD
            opt_used = tf_privacy.DPKerasSGDOptimizer
        else:
            opt_msg = val[0]
            opt_used = val[1]

        opt = opt_used(
            l2_norm_clip=self.l2_norm_clip,
            noise_multiplier=self.noise_multiplier,
            num_microbatches=self.num_microbatches,
            learning_rate=self.learning_rate,
        )

        if len(opt_msg) > 0:
            print(f"During compilation: {opt_msg}")

        super().compile(opt, loss, metrics)

    def fit(
        self,
        X: Any,
        Y: Any,
        validation_data: Any,
        epochs: int,
        batch_size: int,
        refine_epsilon: bool = False,
    ) -> Any:
        """Over-rides the tensorflow fit() method with some
        extra functionality:
         (i) records number of samples for checking DP epsilon values
         (ii) does an automatic epsilon check and reports
         (iia) if user sets refine_epsilon = true, retiurn w ithout fitting the model
         (iii) then calls the tensorflow fit() function
         (iv) finally makes a saved copy of the newly fitted model
        """

        self.num_samples = X.shape[0]
        self.epochs = epochs
        self.batch_size = batch_size
        # make sure you are passing keywords through - but also checking batch size epochs
        ok, msg = self.check_epsilon(X.shape[0], batch_size, epochs)

        if not ok:
            print(msg)
        if refine_epsilon:
            print(
                "Not continuing with fitting model, "
                "as return epsilon was above max recomneded value, "
                "and user set refine_epsilon= True"
            )
            return None
        else:
            returnval = super().fit(
                X,
                Y,
                validation_data=validation_data,
                epochs=epochs,
                batch_size=batch_size,
            )

            # make a saved copy for later analysis
            if not os.path.exists("tfsaves"):
                os.mkdir("tfsaves")
            self.save("tfsaves/fit_model.tf")
            self.saved_was_dpused, self.saved_reason = self.check_DP_used(
                self.optimizer
            )
            self.saved_epsilon = self.current_epsilon
            return returnval

    def posthoc_check(self, verbose: bool = True) -> tuple[str, bool]:
        """Checks whether model should be considered unsafe
        foer exanmple, has been changed since fit() was last run,
        or does not meet DP policy
        """

        # have the model architecture or weights been changed?
        self.save("tfsaves/requested_model.tf")
        models_same, same_msg = test_checkpoint_equality(
            "tfsaves/fit_model.tf",
            "tfsaves/requested_model.tf",
        )
        if not models_same:
            print(f"Recommendation is not to release because {same_msg}.\n")
            return same_msg, True

        # was a dp-enbled optimiser provided?
        allowed, allowedmessage = self.check_optimizer_allowed(self.optimizer)
        if not allowed:
            print(f"Recommendation is not to release because {allowedmessage}.\n")
            return allowedmessage, True

        # was the dp-optimiser used during fit()
        dpused, dpusedmessage = self.check_DP_used(self.optimizer)
        if not dpused:
            print(f"Recommendation is not to release because {dpusedmessage}.\n")
            return dpusedmessage, True

        # is this the same as the values immediately after fit()?
        if (
            dpused != self.saved_was_dpused
            or dpusedmessage != self.saved_reason
            or self.saved_epsilon != self.current_epsilon
        ):
            return "Optimizer config has been changed since training.", True

        # if so what was the value of epsilon achieved
        eps_met, current_epsilon = self.dp_epsilon_met(
            num_examples=self.num_samples,
            batch_size=self.batch_size,
            epochs=self.epochs,
        )
        if not eps_met:
            dpepsilonmessage = (
                f"WARNING: epsilon {current_epsilon} "
                "is above normal max recommended value.\n"
                "Discussion with researcher needed.\n"
            )
            print(
                f"Recommendation is further discussion needed " f"{dpepsilonmessage}.\n"
            )
            return dpepsilonmessage, True
        else:
            print("Recommendation is to allow release.\n")
            dpepsilonmessage = (
                "Recommendation: Allow release.\n"
                f"Epsilon vale of model {current_epsilon} "
                "is below normal max recommended value.\n"
            )

            return dpepsilonmessage, False

    def save(self, name: str = "undefined") -> None:
        """Writes model to file in appropriate format.

        Parameters
        ----------

        name: string
             The name of the file to save

        Returns
        -------

        Notes
        -----

        No return value


        Optimizer is deliberately excluded.
        To prevent possible to restart training and thus
        possible back door into attacks.
        """

        self.model_save_file = name
        while self.model_save_file == "undefined":
            self.model_save_file = input(
                "Please input a name with extension for the model to be saved."
            )

        thename = self.model_save_file.split(".")
        # print(f'in save(), parsed filename is {thename}')
        if len(thename) == 1:
            print("file name must indicate type as a suffix")
        else:
            suffix = self.model_save_file.split(".")[-1]

            if suffix in ("h5", "tf"):
                try:
                    tf.keras.models.save_model(
                        self,
                        self.model_save_file,
                        include_optimizer=False,
                        # save_traces=False,
                        save_format=suffix,
                    )

                except Exception as er:
                    print(f"saving as a {suffix} file gave this error message:  {er}")
            else:
                print(
                    f"{suffix} file suffix  not supported "
                    f"for models of type {self.model_type}.\n"
                )

    def load(self, name: str = "undefined") -> None:
        """
        reads model from file in appropriate format.
        Optimizer is deliberately excluded in the save.
        This is to prevent possibility of restarting training,
        which could offer possible back door into attacks.
        Thus optimizer cannot be loaded.
        """

        self.model_load_file = name
        while self.model_load_file == "undefined":
            self.model_save_file = input(
                "Please input a name with extension for the model to load."
            )
        if self.model_load_file[-3:] == ".h5":
            # load from .h5
            f = tf.keras.models.load_model(
                self.model_load_file, custom_objects={"Safe_KerasModel": self}
            )

        elif self.model_load_file[-3:] == ".tf":
            # load from tf
            f = tf.keras.models.load_model(
                self.model_load_file, custom_objects={"Safe_KerasModel": self}
            )

        else:
            suffix = self.model_load_file.split(".")[-1]
            print(f"loading from a {suffix} file is currently not supported")

        return f
