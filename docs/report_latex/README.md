# 大作业报告 LaTeX 源码

`docs/AuroraDB_大作业报告.pdf` 由本目录编译生成。

| 文件 | 内容 |
| --- | --- |
| `main.tex` | 报告正文、TikZ 图（架构、流程、UML、计划树、三道关卡）、pgfplots 柱状图、参考文献 |
| `se_tables.tex` | 用例、功能 / 非功能需求、HTTP 接口、测试层次、55 个测试用例、需求追踪矩阵、已知缺陷、迭代计划的表格行，由 `main.tex` 引入，可直接编辑 |
| `fig/` | 工作台截图 |

## 编译

需要 XeLaTeX（TeX Live、MiKTeX 均可），在本目录执行两遍，让目录和交叉引用更新：

```bash
xelatex main.tex
xelatex main.tex
```

也可以用 [Tectonic](https://tectonic-typesetting.github.io/)，它会自动下载所需宏包并重复编译：

```bash
tectonic -X compile main.tex
```

字体按 Windows 设置：中文用 ctex 的 `fontset=windows`（宋体、黑体、楷体），西文用 Times New Roman、Arial，代码用 Consolas。在 macOS 上编译时，把第一行的 `fontset=windows` 改为 `fontset=mac`，并把 `\setmainfont`、`\setsansfont`、`\setmonofont` 换成本机已有的字体。

编译完成后，把生成的 `main.pdf` 复制为 `docs/AuroraDB_大作业报告.pdf`。
