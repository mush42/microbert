local num_layers = std.parseInt(std.extVar("NUM_LAYERS"));
// Note this invariant: token embedding dim must be divisible by number of attention heads
local token_embedding_dim = std.parseInt(std.extVar("EMBEDDING_DIM"));
local num_attention_heads = std.parseInt(std.extVar("NUM_ATTENTION_HEADS"));
local embedding_dim = token_embedding_dim; //+ char_embedding_dim;
local pos_embedding_dim = 50;
local tokenizer_path = std.extVar("TOKENIZER_PATH");

// --------------------------------------------------------------------------------
// Reader setup
// --------------------------------------------------------------------------------
local readers = std.parseJson(std.extVar("readers"));

// --------------------------------------------------------------------------------
// Data path setup
// --------------------------------------------------------------------------------
local train_data_paths = std.parseJson(std.extVar("train_data_paths"));
local dev_data_paths = std.parseJson(std.extVar("dev_data_paths"));

// --------------------------------------------------------------------------------
// Head setup
// --------------------------------------------------------------------------------
local xpos_head = {
    "type": "xpos",
    "embedding_dim": embedding_dim,
    "use_crf": false,
    "use_decoder": false
};
local mlm_head = {
  "type": "mlm",
  "embedding_dim": embedding_dim
};
local parser_head = {
    "type": "biaffine_parser",
    "embedding_dim": embedding_dim,
    "encoder": {
        "type": "pass_through",
        "input_dim": embedding_dim + pos_embedding_dim
    },
    //"encoder": {
    //    "type": "stacked_bidirectional_lstm",
    //    "input_size": embedding_dim + pos_embedding_dim,
    //    "hidden_size": (embedding_dim + pos_embedding_dim) / 2,
    //    "num_layers": 1,
    //    "recurrent_dropout_probability": 0.3,
    //    "use_highway": true
    //},
    "pos_tag_embedding": {
        "embedding_dim": pos_embedding_dim,
        "vocab_namespace": "xpos_tags",
        "sparse": false
    },
    "use_mst_decoding_for_validation": true,
    "arc_representation_dim": 500,
    "tag_representation_dim": pos_embedding_dim,
    "dropout": 0.3,
    "input_dropout": 0.3,
    "initializer": {
        "regexes": [
            [".*projection.*weight", {"type": "xavier_uniform"}],
            [".*projection.*bias", {"type": "zero"}],
            [".*tag_bilinear.*weight", {"type": "xavier_uniform"}],
            [".*tag_bilinear.*bias", {"type": "zero"}],
            [".*weight_ih.*", {"type": "xavier_uniform"}],
            [".*weight_hh.*", {"type": "orthogonal"}],
            [".*bias_ih.*", {"type": "zero"}],
            [".*bias_hh.*", {"type": "lstm_hidden_bias"}]
        ]
    }
};

local heads = (
  (if std.parseJson(std.extVar("XPOS")) then {"xpos": xpos_head} else {})
  + (if std.parseJson(std.extVar("MLM")) then {"mlm": mlm_head} else {})
  + (if std.parseJson(std.extVar("PARSER")) then {"parser": parser_head} else {})
);

local xpos = std.parseJson(std.extVar("XPOS"));
local mlm = std.parseJson(std.extVar("MLM"));
local parser = std.parseJson(std.extVar("PARSER"));
local weights = (
  (if mlm then {"mlm": 8} else {})
  + (if xpos then {"xpos": 1} else {})
  + (if parser then {"parser": 1} else {})
);

// scheduling
local batch_size = 32;
local instances_per_epoch = 256000;
// BERT base batch size was 256, trained for 1M steps, so try to match this by half
local BERT_base_total_instances = 256000000;
local batches_per_epoch = instances_per_epoch / batch_size;
// We want to use the full amount, but 200 is a practical limit
local num_epochs = 200; // BERT_base_total_instances / instances_per_epoch;
local plateau = {
    "type": "reduce_on_plateau",
    "factor": 0.5,
    "mode": "min",
    "patience": 2,
    "verbose": true,
    "min_lr": 5e-5
};

local pattern = (
  (if mlm then ["mlm", "mlm", "mlm", "mlm"] else [])
  + (if xpos then ["xpos"] else [])
  + (if mlm then ["mlm", "mlm", "mlm", "mlm"] else [])
  + (if parser then ["parser"] else [])
);

local data_loader = {
    "type": "multitask",
    "scheduler": {
        "type": "homogeneous_weight_proportional",
        "batch_size": batch_size,
        "weights": weights,
        "pattern": pattern
    },
    "shuffle": true,
    "sampler": {
        "type": "weighted",
        "weights": weights
    },
    "instances_per_epoch": instances_per_epoch
};
local validation_data_loader = {
    "type": "multitask",
    "shuffle": true,
    "scheduler": {
        "type": "homogeneous_roundrobin",
        "batch_size": batch_size
    }
};


{
    "dataset_reader" : {
        "type": "multitask",
        "readers": readers
    },
    "data_loader": data_loader,
    "validation_data_loader": validation_data_loader,
    "train_data_path": train_data_paths,
    "validation_data_path": dev_data_paths,
    "model": {
        "type": "multitask",
        "backbone": {
            "type": "bert",
            "embedding_dim": embedding_dim,
            "feedforward_dim": embedding_dim * 4,
            "num_layers": num_layers,
            "num_attention_heads": num_attention_heads,
            "position_embedding_type": "relative_key",
            "tokenizer_path": tokenizer_path
        },
        "heads": heads
    },
    "trainer": {
        "type": "mtl",
        "optimizer": {
            "type": "adamw",
            "lr": 3e-3,
            "betas": [0.9, 0.999],
            "weight_decay": 0.05
            //"type": "multi",
            //"optimizers": {
            //    "biaffine_parser": {"type": "dense_sparse_adam", "betas": [0.9, 0.9]},
            //    "default": {"type": "huggingface_adamw", "lr": 3e-3},
            //},
            //"parameter_groups": [
            //    [[".*biaffine_parser.*"], {"optimizer_name": "biaffine_parser"}]
            //]
        },
        "learning_rate_scheduler": plateau,
        "patience": 100,
        "num_epochs": num_epochs,
        "validation_metric": "-mlm_perplexity",
        "callbacks": [
            {
                "type": "console_logger"
            },
            {
                "type": "tensorboard"
            },
            //{
            //    "type": "should_validate_callback",
            //    "validation_start": 100,
            //    "validation_interval": 1
            //}
        ]
    }
}
