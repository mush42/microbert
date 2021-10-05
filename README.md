# Usage

## Setup
1. Create a new environment:

```bash
conda create --name embur python=3.9
conda activate embur
```

2. Install PyTorch etc. 
```bash
conda install pytorch torchvision cudatoolkit=11.2 -c pytorch
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

## Experiments

### Adding a language
For each new language to be added, you'll want to follow these conventions:

1. Put all data under `data/$NAME/`, with "raw" data going in some kind of subdirectory. 
   (If it is a UD corpus, the standard UD name would be good, e.g. `data/coptic/UD_Coptic-Scriptorium`)
2. Put a script at `embur/scripts/$NAME_data_prep.py` that will take the dataset's native format and 
   write it out into `data/$NAME/converted`. 
   (Note that this script is a submodule of the top-level package `embur`.
   To invoke it, you'll write `python embur.scripts.$NAME_data_prep`.)

### Running experiments

1. 
