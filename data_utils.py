import librosa
import librosa.display

import matplotlib.pyplot as plt
import numpy as np
import os

from pathlib import Path
from shutil import copyfile
from IPython.display import Audio

import pandas as pd
import tensorflow as tf

from tensorflow.data import Dataset
import json

import glob
import functools

import datetime
from pathlib import Path
from collections import defaultdict
import itertools


@functools.lru_cache()
def get_maps(path="meta_train.csv"):
  label_df = pd.read_csv(path)
  label_map = defaultdict(int)
  remark_map = defaultdict(str)
  for idx, (filename, label, remark) in label_df.iterrows():
    assert filename not in label_map
    label_map[filename] = label
    remark_map[filename] = remark
  return {
    "label_map":label_map,
    "remark_map":remark_map,
  }

# @print_func_start_running_to_log
def load_wav(
  path:str,
  sr:int=22050,
  maps:dict=None,
)->[np.array,int, int]:
    """
    load wav from path
    output :
      [signal:np.array, sr:int, label:int]
    """
    maps = maps or get_maps()
    label_map = maps["label_map"]
    remark_map = maps["remark_map"]
    
    x, sr = librosa.load(path,sr=sr)
    filename = path.split("/")[-1].split('.')[0]
    label = label_map[filename]
    remark = remark_map[filename]
    return x, sr, label, remark

class loop_dict_wrapper_class(object):
  def __init__(self, func, **kwargs_need_to_loop):
    self.kwargs_need_to_loop = kwargs_need_to_loop
    self.func = func
    for key,value in kwargs_need_to_loop.items():
      assert "__iter__" in dir(value), f"{value} is not iterable (key:{key})"

  def __setitem__(self, key, value):
    assert "__iter__" in dir(value), f"{value} is not iterable (key:{key})"
    self.get_dict()[key] = value
  
  def __getitem__(self, key):
    return self.get_dict()[key]
  
  def __delitem__(self, key):
    del self.get_dict()[key]
  
  def __iter__(self):
    D = self.get_dict()
    return iter([{k:v for k,v in zip(D.keys(), values_product)} for values_product in itertools.product(*D.values())])
  
  def __call__(self, *args, **kwargs):
    ret = [self.func(*args, **kwargs, **additional_kwargs) for additional_kwargs in self]
    return ret
  def get_dict(self):
    return self.kwargs_need_to_loop
  
# @print_func_start_running_to_log
def wav_to_mel(x:np.array, sr:int=22050, display:bool=False)->np.array:
  melspec = librosa.power_to_db(librosa.feature.melspectrogram(x, sr=sr, n_mels=128))
  if display:
    librosa.display.specshow(melspec, sr=sr, x_axis="time", y_axis="mel")
    plt.colorbar();
    plt.show()
  return melspec

def pitch_shift(x, n_steps=None, sr=22050):
  if n_steps == None:
    return x
  augmented = librosa.effects.pitch_shift(x, sr=sr, n_steps=n_steps)
  return augmented

def process(path):
  pitch_shift_n_steps_5 = loop_dict_wrapper_class(
    pitch_shift,
    n_steps=range(-5,5+1)
  )
  
  ret = {}
  ret["filepath"] = path
  x, _, label, remark = ret["x"], _, ret["label"], ret["remark"] = load_wav(path)
  x_pitch_shift = ret["x_pitch_shift"] = pitch_shift_n_steps_5(x)
  mel = ret["mel"] = wav_to_mel(x)
  pitch_shifted_mel =ret["pitch_shifted_mel"] = [wav_to_mel(x) for x in x_pitch_shift]
  return ret

@functools.lru_cache(1)
def load_dataset_from_folder(folder="train",file_exteionsion="wav"):
  import multiprocessing
  pool_map = multiprocessing.Pool(processes=multiprocessing.cpu_count())
  path_list = sorted(glob.glob(f"{folder}/*.{file_exteionsion}"))
  TRAIN_DICT = dict(zip(path_list, pool_map.map(process, path_list)))
  def generator():
    for filename, d in TRAIN_DICT.items():
      import pdb;pdb.set_trace()
      yield (
        d["filepath"],
        d["x"],
        d["x_pitch_shift"],
        d["mel"],
        d["pitch_shifted_mel"],
        d["label"],
        d["remark"],
      )
  
  train_ds = Dataset.from_generator(
    generator,
    output_types=(
      tf.string,   # filepath
      tf.float32,  # x
      tf.float32,  # x_pitch_shift
      tf.float32,  # mel
      tf.float32,  # pitch_shifted_mel
      tf.int64, # label
      tf.string,# remark
    )
  )
  return train_ds
  
# The following functions can be used to convert a value to a type compatible
# with tf.train.Example.

def _bytes_feature(value):
  """Returns a bytes_list from a string / byte."""
  if isinstance(value, type(tf.constant(0))):
    value = value.numpy() # BytesList won't unpack a string from an EagerTensor.
  return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))

def _float_feature(value):
  """Returns a float_list from a float / double."""
  return tf.train.Feature(float_list=tf.train.FloatList(value=[value]))

def _int64_feature(value):
  """Returns an int64_list from a bool / enum / int / uint."""
  return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))

def serialize_example(filepath, x, x_pitch_shift, mel, pitch_shifted_mel, label, remark):
  feature = {
    "filepath":
      _bytes_feature(filepath.numpy()),
    "x_byte_string":
      _bytes_feature(x.numpy().astype(np.float32).tostring()),
    "x_pitch_shift_byte_string":
      _bytes_feature(x_pitch_shift.numpy().astype(np.float32).tostring()),
    "mel_byte_string":
      _bytes_feature(mel.numpy().astype(np.float32).tostring()),
    "pitch_shifted_mel_byte_string":
      _bytes_feature(pitch_shifted_mel.numpy().astype(np.float32).tostring()),
    "label":
      _int64_feature(label.numpy()),
    "remark":
      _bytes_feature(remark.numpy()),
  }
  
  example_proto = tf.train.Example(features=tf.train.Features(feature=feature))
  return example_proto.SerializeToString()

def tf_serialize_example(filepath, x, x_pitch_shift, mel, pitch_shifted_mel, label, remark):
  tf_string = tf.py_function(
    serialize_example,
    (filepath, x, x_pitch_shift, mel, pitch_shifted_mel, label, remark),  # pass these args to the above function.
    tf.string)      # the return type is `tf.string`.
  return tf.reshape(tf_string, ()) # The result is a scalar

def write_ds_to_tfrecord(
  ds,
  folder=None,
  filename="train.filepath.x.x_pitch_shift.mel.pitch_shifted_mel.label.remark.tfrecord",
  metafilename="meta.json",
):
  folder = folder or datetime.date.today().strftime("tfrecord_%Y_%m_%d")
  folder = Path(folder)
  
  folder.mkdir(parents=True, exist_ok=True)
  
  filepath = folder/filename
  filepath = str(filepath)
  
  metapath = folder/metafilename
  
  writer = tf.data.experimental.TFRecordWriter(filepath)
  writer.write(ds.map(tf_serialize_example).prefetch(10))

  LEN = 0
  for _ in ds:
    LEN+=1
  with open(metapath, "w") as f:
    (example_filepath,
     example_x,
     example_x_pitch_shift,
     example_mel,
     example_pitch_shifted_mel,
     example_label,
     example_remark) = next(iter(ds))
    json.dump(
      fp=f,
      obj = {
        "x_shape":example_x.shape.as_list(),
        "x_pitch_shift_shape":example_x_pitch_shift.shape.as_list(),
        "mel_shape":example_mel.shape.as_list(),
        "pitch_shifted_mel_shape":example_pitch_shifted_mel.shape.as_list(),
        "total_files" : LEN,
      },
      indent=2,
    )
  
  copyfile(Path(__file__).absolute(), folder/(__file__+".copy"))

def get_latest_tfrecord_parent(prefix="tfrecord"):
  folders = sorted(glob.glob(f"{prefix}*"), key=os.path.getmtime, reverse=True)
  assert len(folders) > 0
  return folders[0]


def load_TFRecord(
  folder=None,
  tfrecord_name="train.filepath.x.x_pitch_shift.mel.pitch_shifted_mel.label.remark.tfrecord",
  meta_name="meta.json",
):
  folder = folder or get_latest_tfrecord_parent()
  filepaths = [str(Path(folder)/tfrecord_name)]
  raw_dataset = tf.data.TFRecordDataset(filepaths)
  meta = json.load(open(Path(folder)/meta_name, "r"))
  
  feature_description = {
      "filepath":tf.io.FixedLenFeature([], tf.string, default_value=""),
      "x_byte_string":tf.io.FixedLenFeature([], tf.string, default_value=""),
      "x_pitch_shift_byte_string":tf.io.FixedLenFeature([], tf.string, default_value=""),
      "mel_byte_string":tf.io.FixedLenFeature([], tf.string, default_value=""),
      "pitch_shifted_mel_byte_string":tf.io.FixedLenFeature([], tf.string, default_value=""),
      'label': tf.io.FixedLenFeature([], tf.int64, default_value=0),
      "remark":tf.io.FixedLenFeature([], tf.string, default_value=""),
  }

  def _parse_function(example_proto):
    # Parse the input `tf.train.Example` proto using the dictionary above.
    return tf.io.parse_single_example(example_proto, feature_description)

  def post_processing(DICT):
    ret_list = []
    numerical_keyword = "_byte_string"
    for key in ['filepath',
                'x_byte_string',
                'x_pitch_shift_byte_string',
                'mel_byte_string',
                'pitch_shifted_mel_byte_string',
                'label',
                'remark']:
      value = DICT.get(key)
      if numerical_keyword in key:
        value = tf.io.decode_raw(value, out_type=tf.float32)
        value = tf.reshape(value, meta[key.split(numerical_keyword)[0]+"_shape"])
      ret_list.append(value)
    return ret_list
  train_ds_from_tfrecord = raw_dataset.map(_parse_function).map(post_processing)
  
  return train_ds_from_tfrecord, meta

def mix_mels(mels):
  # 10*np.log10((10**(melspec_x/10)+10**(melspec_x2/10)))
  
  inverse_fun = lambda mel:10**(mel/10)
  convert_fun = lambda S_db:10*np.log10(S_db)
  
  ret = convert_fun(sum(map(inverse_fun, mels)))
  return ret

def main():
  train_ds = load_dataset_from_folder()
  write_ds_to_tfrecord(train_ds)
  tfrecord_ds, meta = load_TFRecord()
  print(tfrecord_ds)
  for sample in tfrecord_ds:
    print(sample)
    break
  
if __name__ == "__main__":
  main()