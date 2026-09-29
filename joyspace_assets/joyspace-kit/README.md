# @jd/joyspace-kit

JoySpace 技能套件 for Claude Code — 读取、搜索、列表、上传图片、导入 Markdown 文档、读取在线表格、上传 xlsx 建在线表格。

## 安装

```bash
# 设置京东内网 npm 源（仅 @jd scope）
npm config set @jd:registry http://registry.m.jd.com/

# 全局安装
npm i -g @jd/joyspace-kit

# 在项目目录中初始化（安装 CLAUDE.md 技能指令）
cd your-project
joyspace-kit init
```

初始化后，`CLAUDE.md` 中会注入 JoySpace 操作指令，Claude Code 启动时自动加载。

## 前置条件

- **Node.js >= 18**
- **HiOffice 桌面端运行中**（端口 8988-9006），用于自动获取认证 token
- **内网或 VPN**，需能访问 `apijoyspace.jd.com`

## 包含的技能

| 技能 | 用途 | 命令示例 |
|------|------|----------|
| **joyspace-read** | 读取 JoySpace 文档为 Markdown | `node $JOYSPACE_KIT/joyspace-read-doc/scripts/read_joyspace_doc.js --url <url>` |
| **joyspace-search** | 全文搜索 JoySpace 文档 | `node $JOYSPACE_KIT/joyspace-search/scripts/search_joyspace.mjs --query "关键词"` |
| **joyspace-list** | 列出最近访问的 JoySpace 页面 | `node $JOYSPACE_KIT/joyspace-list/scripts/list_joyspace.mjs` |
| **joyspace-list-folder** | 递归列出团队空间/文件夹下所有文档 | `node $JOYSPACE_KIT/joyspace-list-folder/scripts/list_folder.mjs --url <folder-url>` |
| **joyspace-upload-image** | 上传本地图片到 JoySpace CDN | `node $JOYSPACE_KIT/joyspace-upload-image/scripts/upload_image.mjs --file img.png --auto-scratch` |
| **markdown-to-joyspace** | 导入 Markdown 文件为 JoySpace 文档 | `node $JOYSPACE_KIT/markdown-to-joyspace/scripts/import_markdown_doc.js --file doc.md` |
| **joyspace-read-sheet** | 读取 JoySpace 在线表格为结构化 JSON | `node $JOYSPACE_KIT/joyspace-read-sheet/scripts/read_joyspace_sheet.mjs --url <sheets-url>` |
| **xlsx-to-joyspace** | 上传本地 xlsx/xls/csv 为 JoySpace 在线表格 | `node $JOYSPACE_KIT/xlsx-to-joyspace/scripts/upload_joyspace_sheet.mjs --file data.xlsx` |
| **hioffice-auth** | 获取认证 token（其他脚本自动调用） | `node $JOYSPACE_KIT/hioffice-auth/scripts/hioffice-auth.mjs` |

## CLI 命令

```bash
joyspace-kit init          # 在当前项目安装 CLAUDE.md 技能指令
joyspace-kit init --force  # 强制覆盖已有的 joyspace-kit 区段
joyspace-kit path          # 输出包安装路径
joyspace-kit auth          # 测试 HiOffice 认证是否正常
joyspace-kit help          # 显示帮助
```

## 使用示例

### 读取文档并总结
```
你: "帮我看看这个文档 https://joyspace.jd.com/pages/xxx 在说什么"
Claude Code: 自动调用 joyspace-read，读取内容并总结
```

### 搜索文档
```
你: "搜一下全域通相关的文档"
Claude Code: 自动调用 joyspace-search，返回匹配结果
```

### 导入 Markdown
```
你: "把这个文件发布到 JoySpace：./notes.md"
Claude Code: 自动调用 markdown-to-joyspace，返回创建的文档 URL
```

### 读在线表格
```
你: "把这个表格取数出来 https://joyspace.jd.com/sheets/xxx"
Claude Code: 自动调用 joyspace-read-sheet，返回结构化 rows[][]
```

### 上传 xlsx 建在线表格
```
你: "把这份 xlsx 发到 JoySpace 变成在线表格：./report.xlsx"
Claude Code: 自动调用 xlsx-to-joyspace，返回新表格 URL
注意：新上传的表格需要在浏览器打开一次才能被 joyspace-read-sheet 读到（表格引擎懒初始化）
```

## 认证说明

所有脚本通过本地 HiOffice 客户端自动认证，认证链路：

1. `ME_TOKEN` / `SSO_TOKEN` 环境变量（如已设置）
2. `~/.joyclaw/openclaw.json` cookies
3. `JMECHAT_token` via tokenGrant
4. 本地 HiOffice legacy exchange（端口 8988-9006）

**如果遇到 `NEED_ACCESS_RIGHT` 或 `PAGE NOT EXIST`：** 说明当前认证账号没有该文档的访问权限，不是脚本 bug。需要文档作者授权，或在 HiOffice 中切换到有权限的账号。

## 项目结构

```
joyspace-kit/
├── bin/cli.js                      # CLI 入口 (joyspace-kit init/path/auth)
├── CLAUDE.md                       # Claude Code 技能指令
├── package.json
├── README.md
└── skills/
    ├── shared-auth.mjs             # 共享认证模块
    ├── hioffice-auth/scripts/      # HiOffice 认证
    ├── joyspace-read-doc/          # 读取文档 (+ references)
    ├── joyspace-search/scripts/    # 搜索文档
    ├── joyspace-list/scripts/      # 列出最近文档
    ├── joyspace-list-folder/       # 递归列出文件夹树
    ├── joyspace-upload-image/      # 上传图片 (+ references)
    ├── markdown-to-joyspace/       # 导入 Markdown (+ references, scripts)
    ├── joyspace-read-sheet/        # 读在线表格 (+ references)  ★ 0.3.0 新增
    └── xlsx-to-joyspace/           # 上传 xlsx 建在线表格 (+ references)  ★ 0.3.0 新增
```

## License

MIT
