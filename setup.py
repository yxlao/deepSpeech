from setuptools import setup, find_packages
from pip.req import parse_requirements

install_reqs = parse_requirements('requirements.txt', session='hack')
reqs = [str(ir.req) for ir in install_reqs]

setup(name='deepspeech',
      version='0.0.1',
      install_requires=reqs,
      description='DeepSpeech 2 TensorFlow Implementation',
      packages=find_packages(exclude="tests"))
