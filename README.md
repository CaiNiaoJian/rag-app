<div align="center">

# DocFactory

Windows 离线文档解析与数据集生产工具

[下载安装包](https://github.com/ohaoz/rag-app/releases/latest) · [全部版本](https://github.com/ohaoz/rag-app/releases) · [开发文档](docs/README.md)

</div>

导入 doc / docx / pdf / ppt / pptx / xls / xlsx，解析为保留结构的 Markdown，按语义切片，导出为 MD / PDF / JSON / CSV 或 Alpaca / ShareGPT 微调数据集。用于 RAG 知识库建设与本地模型语料生产。运行全程离线，数据不出本机。

## 功能

- 七种格式导入，拖拽文件或文件夹，批量队列处理，支持取消与重试
- 解析保留标题层级、表格结构（含合并单元格）、图片与页码出处
- 结构感知切片：表格不截断、标题跟随正文，切片长度与重叠可调，支持重切
- 导出 Markdown（含图片资产）、PDF、切片 JSON、CSV、Alpaca、ShareGPT 六种格式
- 每份文档输出文本覆盖率、表格置信度等质量指标，解析质量可量化验证
- 解析降级逐页记录，错误提示给出原因与建议操作，技术详情可展开
- 运行时代码层面禁止一切网络连接，仅本机回环通信

## 安装

1. 从 [Releases](https://github.com/ohaoz/rag-app/releases/latest) 下载 `DocFactory-x.y.z-win-x64-setup.exe`
2. 双击运行，可自选安装目录（默认装到用户目录，该路径免管理员权限）；无需预装 Python、Office 或任何运行库
3. 安装完成自动启动，桌面与开始菜单生成快捷方式

安装包暂未做代码签名，SmartScreen 提示「未知发布者」时，点「更多信息 → 仍要运行」。

更新：应用内「设置 → 关于 → 检查更新」，仅在手动触发时访问 GitHub。

## 版本状态

当前为早期预览版（v0.1.x）。

| | |
|---|---|
| 已可用 | docx / pptx / xlsx / 文本型 PDF / xls 解析；导入 → 解析 → 切片 → 导出完整流程；批量任务队列；六种导出格式；仪表盘与日志查看器 |
| 开发中 | 扫描件与图片 OCR；复杂多栏 PDF 版面还原；doc / ppt 解析；.kmod 离线模组更新 |

## 系统要求

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 x64（1809+）/ Windows 11 x64，不支持 ARM64 |
| CPU | 需支持 AVX2，安装时自动检测 |
| 内存 | 最低 8GB，推荐 16GB |
| 磁盘 | 预留 10GB 以上 |

## 数据与隐私

数据目录为 `%LOCALAPPDATA%\DocFactory`，卸载时默认保留。应用不含遥测，不后台联网。

## 开发

架构设计与需求规格见 [docs/](docs/README.md)。技术栈：Electron + Python sidecar（PyInstaller），契约优先开发。构建与 CI 门禁见 [ci.yml](.github/workflows/ci.yml)。
