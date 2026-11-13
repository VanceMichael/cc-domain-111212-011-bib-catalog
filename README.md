# bib-catalog

文献条目宏与继承关系的采集所用的来源检查与原件留存工具。

## 当前能力

`source.py` 查看来源的现有结构，不把检查结果冒充完整业务采集。
`bibtex.py` 对 BibTeX 做保留来源位置的词法与语义解析：字符串宏、拼接、嵌套花括号、引号与注释由逐字符扫描器处理，不用正则截取；每个位置同时给出字符索引、行号与 UTF-8 字节范围。宏在出现处对后续字段生效且声明时求值，中途改宏对不同条目的影响可由 `macro_usage` 核对。crossref 与 xdata 可前向引用，字段优先级为显式、按 xdata 列表顺序、crossref，被压制的候选值留在字段 `shadowed` 中。重复键、未定义宏与继承环都记入诊断，不任选一份掩盖；单条坏记录只产生诊断，不影响其他条目。每个导出字段标明是自己写的、从哪项继承的，还是仍有未解引用；作者姓名同时保留原表示与检索表示（保护括号、LaTeX 命令）。
`storage.py` 将原始字节、来源标识、摘要和检查结果一起保存在本地 SQLite，重复原件保留同一收件编号；每次解析作为新的解析序号留存，可用不同配置重新解析并对比结果变化。清洗后的检索字符串不会反写覆盖原件。
`sampledata.py` 构造可重复的本地数据；`transport.py` 提供独立的回环 HTTP 演练源与请求记录。
解释范围和来源变体见 `docs/source-contract.md`，当前检查测试与将来新增业务能力的测试应分开保留。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t bib-catalog .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py bibtex.py source.py sampledata.py storage.py transport.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
python3 app.py catalog-demo
```

`demo` 演示真实启动本地来源、领取原件、执行检查并保存临时数据库，展示检查结果与重复收件身份，退出后自动清理。正常在数秒内完成，不以预录日志代替执行。
`catalog-demo` 用含逗号人名、跨条目继承和中途改宏的目录演示解析、继承来源、宏可见范围与重新解析后的结果变化。

## 单独检查与收件

```sh
python3 app.py fixtures /tmp/bib-catalog-fixtures
python3 app.py inspect /tmp/bib-catalog-fixtures/sample.bib
python3 app.py capture /tmp/bib-catalog-fixtures/sample.bib --database /tmp/bib-catalog.sqlite3 --source local-partner
python3 app.py list --database /tmp/bib-catalog.sqlite3
```

生成夹具不会覆盖已有文件。运行错误以 JSON 输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。

## 解析、查询与重新解析

```sh
python3 app.py parse /tmp/bib-catalog-fixtures/sample.bib
python3 app.py capture /tmp/bib-catalog-fixtures/xdata.bib --database /tmp/bib-catalog.sqlite3 --source local-partner
python3 app.py query --database /tmp/bib-catalog.sqlite3 --source local-partner --key paper
python3 app.py query --database /tmp/bib-catalog.sqlite3 --capture 1 --key paper --parse 1
python3 app.py reparse --database /tmp/bib-catalog.sqlite3 --capture 2 --no-xdata
python3 app.py parses --database /tmp/bib-catalog.sqlite3 --capture 2
```

`capture` 在收件时保存第一次解析；`query` 按来源（默认最新收件，可用 `--capture` 指定）与条目键返回字段级来源；`reparse` 以新配置重新解析保留的原件，逐字段解释结果变化；`parses` 列出某收件的全部解析序号与配置。
