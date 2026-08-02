# AstrBot 插件存放目录

把每个 AstrBot Star 插件（含 `main.py`、`metadata.yaml`、`_conf_schema.json`）
作为一个独立子目录放在这里。桥（`../mofox_astr_compat`）启动时会自动扫描本
目录下所有子目录并批量加载。

例如：

```
E:\plugins\1\MofoxForAstrPulgins\
├── mofox_astr_compat\          # 兼容桥本体
└── plugins\                    # ← 本目录，放被兼容的 AstrBot 插件
    ├── astrbot_plugin_hello\
    │   ├── main.py
    │   ├── metadata.yaml
    │   └── _conf_schema.json
    └── astrbot_plugin_toolbox\
        └── main.py
```

也可以在桥配置中用 `plugin.plugin_path` 指定单个插件目录，或用
`plugin.plugins_root` 指向其他目录。
