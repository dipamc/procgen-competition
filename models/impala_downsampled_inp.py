from ray.rllib.models.tf.tf_modelv2 import TFModelV2
from ray.rllib.utils.framework import try_import_tf
from ray.rllib.models import ModelCatalog

tf = try_import_tf()


def conv_layer(depth, name):
    return tf.keras.layers.Conv2D(
        filters=depth, kernel_size=3, strides=1, padding="same", name=name
    )


def residual_block(x, depth, prefix, extra_inputs=None):
    inputs = x
    assert inputs.get_shape()[-1].value == depth
    x = tf.keras.layers.ReLU()(x)
    if extra_inputs is not None:
        x = tf.keras.layers.Concatenate()([x, extra_inputs])
    x = conv_layer(depth, name=prefix + "_conv0")(x)
    x = tf.keras.layers.ReLU()(x)
    x = conv_layer(depth, name=prefix + "_conv1")(x)
    return x + inputs


def conv_sequence(x, depth, prefix, pool=True, extra_inputs=None):
    x = conv_layer(depth, prefix + "_conv")(x)
    if pool:
        x = tf.keras.layers.MaxPool2D(pool_size=3, strides=2, padding="same")(x)
    x = residual_block(x, depth, prefix=prefix + "_block0", extra_inputs=extra_inputs)
    x = residual_block(x, depth, prefix=prefix + "_block1")
    return x


class ImpalaDSCNN(TFModelV2):
    """
    Network from IMPALA paper implemented in ModelV2 API.

    Based on https://github.com/ray-project/ray/blob/master/rllib/models/tf/visionnet_v2.py
    and https://github.com/openai/baselines/blob/9ee399f5b20cd70ac0a871927a6cf043b478193f/baselines/common/models.py#L28
    """

    def __init__(self, obs_space, action_space, num_outputs, model_config, name):
        super().__init__(obs_space, action_space, num_outputs, model_config, name)

        depths = [32, 64, 64]

        inputs = tf.keras.layers.Input(shape=obs_space.shape, name="observations")
        scaled_inputs = tf.cast(inputs, tf.float32) / 255.0

        x = scaled_inputs
        x = tf.keras.layers.AveragePooling2D(2,2)(x)
#         x, prev = x[...,-3:], x[...,:-3]
#         prev = tf.stop_gradient(prev)
        x = tf.stop_gradient(x)
        x = conv_sequence(x, depths[0], prefix="seq0", pool=False)
        x = conv_sequence(x, depths[1], prefix="seq1", pool=True)
        x = conv_sequence(x, depths[2], prefix="seq2", pool=True)


        x = tf.keras.layers.Flatten()(x)
        x = tf.keras.layers.ReLU()(x)
        x = tf.keras.layers.Dense(units=256, name="project")(x)
        x = tf.keras.layers.LayerNormalization(name='lnorm')(x)
        x = tf.keras.layers.Activation('tanh', name='hidden')(x)
        logits = tf.keras.layers.Dense(units=num_outputs, name="pi")(x)
        value = tf.keras.layers.Dense(units=1, name="vf")(x)
        self.base_model = tf.keras.Model(inputs, [logits, value])
        self.register_variables(self.base_model.variables)

    def forward(self, input_dict, state, seq_lens):
        # explicit cast to float32 needed in eager
        obs = tf.cast(input_dict["obs"], tf.float32)
        logits, self._value = self.base_model(obs)
        return logits, state

    def value_function(self):
        return tf.reshape(self._value, [-1])


# Register model in ModelCatalog
ModelCatalog.register_custom_model("impala_downsampled_inp", ImpalaDSCNN)
