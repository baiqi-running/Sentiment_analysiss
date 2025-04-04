from setuptools import setup, find_packages

setup(
    name="sentiment_analysis",
    version="0.1",
    packages=find_packages(),
    install_requires=[
        'torch>=2.2.1',
        'torchvision>=0.17.1',
        'transformers>=4.37.2',
        'scikit-learn>=1.4.0',
        'pandas>=2.2.0',
        'numpy>=1.26.3',
        'matplotlib>=3.8.2',
        'seaborn>=0.13.1',
        'tqdm>=4.66.1',
        'pillow>=10.2.0'
    ]
) 