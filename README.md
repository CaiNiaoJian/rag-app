# DocFactory

离线的 Windows 桌面工具，把 Word、PDF、PPT、Excel 批量解析成干净的 Markdown 和结构化数据，拿去建 RAG 知识库、做微调语料，或者单纯把老文档整理成能用的东西。

**下载**：[Releases 页面](https://github.com/ohaoz/rag-app/releases/latest)，装 `DocFactory-x.y.z-win-x64-setup.exe`。双击就装好了，不需要管理员权限，也不用预装 Python 或 Office。

第一次运行 Windows 可能弹「未知发布者」的警告——安装包还没做代码签名，点「更多信息」→「仍要运行」就行。

## 能干什么

支持 doc / docx / pdf / ppt / pptx / xls / xlsx。文件或整个文件夹拖进去，排队自动解析，出来的结果保留标题层级、表格结构和页码出处，不是一坨乱掉的纯文本。解析完按语义结构切片（表格不会被拦腰切断，粒度可调），再导出成 Markdown、PDF、JSON、CSV，或者直接生成 Alpaca / ShareGPT 格式的微调数据集。

跟一般解析工具不太一样的地方：

- 每份文档解析完都有量化指标（文本覆盖率、表格置信度这些），好不好心里有数，不用靠肉眼猜。
- 解析失败不是黑盒。降级链逐页记录用到了哪一级，报错是人话加建议操作，技术堆栈折叠在详情里备查。

全程不联网。运行时代码层面禁止一切外联，数据就在你本机（`%LOCALAPPDATA%\DocFactory`，卸载时默认保留）。「设置 → 关于 → 检查更新」也只在你手动点击时才访问 GitHub，不会后台偷偷联网。

## 现在做到哪了

目前是早期预览版（v0.1.x），说实话离完整还有距离。

能用的：docx / pptx / xlsx / 文本型 PDF / xls 的解析；导入 → 解析 → 切片 → 导出全流程；批量任务队列（进度、取消、重试）；六种导出格式；仪表盘和日志查看器。

还没好的：扫描件和图片文字的 OCR、复杂多栏 PDF 的版面还原、老格式 doc / ppt 的解析（这两种现在会直接报错）、离线模组热更新。都在路线图上，别急。

## 系统要求

Windows 10（1809 及以上）或 Windows 11，仅 x64，不支持 ARM。CPU 需要支持 AVX2（2013 年以后的基本都支持，太老的机器安装时会提示）。内存最低 8G、推荐 16G，磁盘留 10G 以上。

## 开发

架构设计、需求规格等完整文档在 [docs/](docs/README.md)（Electron + Python sidecar，契约优先）。本地构建和 CI 门禁见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。
