# HF 模型下载器

HF 模型下载器是一款 Hugging Face 文件下载工具。它可以直接下载单个文件，也可以读取模型或数据集仓库的文件清单，选择所需文件后批量下载；同时提供针对 ComfyUI 模型目录的保存位置选择功能。

当前版本：`1.0.1`

## 主要功能

- 支持 Hugging Face 模型仓库和数据集仓库。
- 支持仓库首页、文件页面、直接下载链接和仓库子目录链接。
- 仓库文件可按文件树或全部文件两种方式查看、筛选和排序。
- 支持分批选择同一仓库中的文件，已确认下载的文件会在本次选择过程中变为不可选状态。
- 支持保留仓库目录结构，或把文件统一下载到同一目录。
- 支持直接下载到普通目录，单个文件不会额外创建仓库名称文件夹。
- 支持下载到 ComfyUI，并根据模型类型定位较合适的模型目录。
- 支持收藏 ComfyUI 目录、新建文件夹和搜索当前目录层级。
- 支持暂停、继续、重试、优先下载、打开文件夹、复制下载链接和打开原始页面。
- 支持任务多选、拖动框选和按 `Delete` 删除所选任务及对应文件。
- 支持 1～8 个文件同时下载，每个文件可选择 8 路或 16 路连接。
- 支持 HTTP 代理和 Hugging Face Token。
- 下载完成后按文件大小及可用的 SHA-256 信息进行校验。

下载由随程序提供的 `aria2c` 执行，桌面程序通过本机回环地址上的临时 JSON-RPC 服务控制下载。该服务只监听 `127.0.0.1`，每次启动使用随机端口和随机密钥。

## 直接使用

1. 打开 [`release/HFDownloader`](release/HFDownloader) 目录。
2. 双击 `HFDownloader.exe`。
3. 把 Hugging Face 链接粘贴到“添加下载”输入框。
4. 选择普通下载或“添加并下载到 ComfyUI”。

发布版采用目录模式，`HFDownloader.exe` 必须与 `_internal` 文件夹放在一起。不要只复制 EXE，也不要删除 `_internal/vendor/aria2c.exe`。

默认下载目录为：

```text
%USERPROFILE%\Downloads\HFDownloader
```

可以在主窗口中修改默认保存位置。

## 使用前操作

### 1. 代理设置

在主窗口顶部的“代理”输入框中填写 HTTP 代理或混合代理端口，例如：

```text
http://127.0.0.1:10808
```

也可以填写：

```text
127.0.0.1:10808
```

程序会自动补全 `http://`。不使用代理时保持为空，程序将使用直连。首次没有已保存的代理设置时，程序会尝试读取当前 Windows 系统代理；之后使用并保存界面中的代理值。

### 2. Hugging Face Token 设置

公开仓库通常无需 Token。以下情况建议填写：

- 下载需要先接受授权条款的模型；
- 下载私有仓库；
- 匿名请求受到频率限制。

点击右上角“Token / 帮助”，按照界面提示创建具有 `Read` 权限的 Hugging Face Token，然后粘贴并保存。

Token 保存在当前 Windows 用户的凭据管理器中，不会写入软件目录、设置文件或下载队列。程序设置和任务记录保存在：

```text
%LOCALAPPDATA%\HF下载器
```

其中主要包括：

- `settings.json`：下载目录、代理、连接数、ComfyUI 路径和收藏目录；
- `queue.json`：任务队列；
- `engine.log`：本地下载引擎日志。

### 3. ComfyUI 目录和模型位置设置

需要把模型下载到 ComfyUI 时，先点击主窗口右上角“设置”：

1. 选择包含 `models` 文件夹的 ComfyUI 根目录。
2. 根据当前 ComfyUI 的目录结构，选择扩散模型存放位置：
   - `models\diffusion_models`；
   - `models\unet`。
3. 点击“保存”。

LoRA、VAE、ControlNet、文本编码器、放大模型、嵌入和主模型等其他文件，程序会根据文件名和仓库路径定位到较合适的模型目录，最终保存位置仍由你在下载前确认。

## 支持的链接

以下链接形式均可识别：

```text
https://huggingface.co/组织名/仓库名
https://huggingface.co/组织名/仓库名/tree/main
https://huggingface.co/组织名/仓库名/tree/main/子目录
https://huggingface.co/组织名/仓库名/blob/main/文件名
https://huggingface.co/组织名/仓库名/resolve/main/文件名
https://huggingface.co/datasets/组织名/仓库名
```

粘贴仓库链接时，程序会先读取文件清单。可以在文件树中选择文件或文件夹，也可以切换到“所有文件”视图后按文件名、路径、大小或下载状态排序。

## 普通下载

点击“添加并下载”后：

- 单个文件直接保存到当前默认目录。
- 仓库或子目录链接会先打开文件选择页面。
- 选择多个文件时，可选择“保留原文件夹结构”或“下载到同一目录”。
- 如果没有一次选完，可使用“确认并选择剩余文件”继续分批选择。
- 目标目录存在同名文件时，程序会让你选择覆盖、分别保存或跳过。

下载中的临时文件使用以下后缀：

```text
.hfdownload
.hfdownload.aria2
.hfdownload.json
```

下载并校验完成后，程序会清理对应的临时记录。

## 下载到 ComfyUI

首次使用前，点击右上角“设置”，选择包含 `models` 文件夹的 ComfyUI 根目录。

程序会根据文件名和仓库路径识别 LoRA、VAE、ControlNet、文本编码器、放大模型、嵌入、扩散模型或主模型等类型。选择一个文件夹或同类型的多个文件时，目录选择器会定位到可能性较高的模型目录；多个文件类型不同或存在无法识别的文件时，会定位到 `models` 根目录。

最终保存位置始终由你确认。保存时可选择：

- 保留原文件夹结构；
- 下载到同一目录。

目录选择器支持收藏目录、新建同级文件夹、逐层搜索以及返回文件选择页面。

## 下载队列操作

- 单击或拖动可选择一个或多个任务。
- `Ctrl+A` 选择全部任务。
- `Delete` 移除所选记录并删除对应文件，执行前会显示确认窗口。
- 双击任务可暂停、继续或重试。
- 右键任务可打开文件夹、设为优先下载、复制下载链接或打开原始下载页面。
- “移除记录”只移除队列记录；“移除并删除文件”还会删除已下载文件和该任务的临时文件。

## 从源码运行

项目使用 Windows 环境。为避免污染系统 Python，请在项目目录创建隔离环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe .\desktop_app\app.py
```

应用运行时只依赖 Python 标准库和随项目提供的 `aria2c.exe`。`PyInstaller` 仅用于构建发布版。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s desktop_app -p test_download.py -q
```

## 构建发布版

构建前需要关闭 `HFDownloader.exe`，并等待正在运行的 aria2 下载结束。然后执行：

```powershell
.\desktop_app\build.ps1
```

构建结果会更新到：

```text
release\HFDownloader
```

如果只需要生成暂存包而不替换现有发布目录：

```powershell
.\desktop_app\build.ps1 -StageOnly
```

暂存包位于：

```text
build\pyinstaller-package-stage\package\HFDownloader
```

构建脚本会把许可证和对应源代码一并放入发布目录。用户设置、Token、队列和下载内容不会打包进发布版。

## 项目结构

```text
desktop_app/
  app.py              桌面界面与任务管理
  core.py             Hugging Face 链接解析、仓库读取和 aria2 RPC
  test_download.py    自动测试
  build.ps1           Windows 发布脚本
  vendor/             aria2c 及第三方许可证材料
release/
  HFDownloader/       可直接运行的发布目录
```

## 许可

本项目原创代码使用 MIT License，详见 [`desktop_app/LICENSE.txt`](desktop_app/LICENSE.txt)。

项目随附 aria2 1.37.0 Windows 64 位可执行文件，并通过本机 JSON-RPC 独立运行。aria2 使用 GNU GPL v2 或更高版本。其他第三方组件及许可证信息见 [`desktop_app/THIRD_PARTY.txt`](desktop_app/THIRD_PARTY.txt) 和发布目录中的 `licenses`、`sources` 文件夹。
