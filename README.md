# astrbot_plugin_image_search

给 AstrBot 的 LLM 一个 `search_image` 工具：**按关键词联网搜图，并把图片直接发到会话里**。

不需要任何 API Key，国内可直连，适合作为"让机器人发图"的通用方案。

## 特性

- **零配置可用**：默认使用 Bing 图片搜索（也可切 360），装完就能用，不需要注册任何账号
- **图源可自定义**：支持任意返回 JSON 的图源（Bing / Pixabay / 自建图床等），填写接口地址和字段映射即可
- **真的会发图**：图片由插件自己下载、校验格式后再以本地文件发送，不依赖平台去拉 URL
- **长查询自动降级**：图源对"多个词堆一起"的查询常常返回空，插件会逐步缩短关键词重试
- **相关性排序**：标题里包含查询词的结果优先发送
- **安全过滤**：默认开启；按标题匹配敏感词，命中即跳过（可关闭）
- **可配张数上限**：防止一次刷屏

## 安装

### 方式一：插件市场

在 AstrBot WebUI 的插件市场搜索 `astrbot_plugin_image_search`。

### 方式二：手动安装

```bash
cd <AstrBot 实例目录>/data/plugins
git clone https://github.com/MaizioO/astrbot_plugin_image_search
```

然后重启 AstrBot。

## 使用

装好后不需要额外操作，直接在对话里说：

```
搜一张布偶猫的图片
来几张埃菲尔铁塔的照片
```

AI 会调用 `search_image` 工具，图片会直接出现在会话里。

> 提示：关键词用**简短的中文名词**效果最好（2-6 个字）。图源对长句子和英文查询的召回很差。

## 配置

WebUI → 插件 → 联网搜图 → 设置。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| 图源 | `360` | `360` 内置免 Key；`custom_json` 自定义 JSON 接口 |
| 默认发送张数 | 2 | 用户没指定时发几张 |
| 单次最多张数 | 5 | 硬上限 10，防止刷屏 |
| 安全过滤 | **开启** | 按标题匹配敏感词后跳过 |
| 自定义敏感词 | 空 | 逗号分隔，追加到内置词表 |
| 优先原图 | 关闭 | 关闭时取缩略图（更快更稳），开启时取原图 |
| 请求超时 | 12 秒 | 搜索与下载共用 |

### 内置图源对比

| | 360 | Bing |
| --- | --- | --- |
| 搜索耗时 | 约 0.5 秒 | 约 3-4 秒 |
| 图片分辨率 | 缩略图（约 100-400 KB） | 原图（常见 300KB-2MB，可达 4K） |
| 是否带标题 | 是 | 是 |
| 下载稳定性 | 自己 CDN，很稳 | 第三方站点，少量图片可能失效 |

默认是 **Bing**（画质优先）；如果更在意响应速度可以切到 **360**。两者都不需要 API Key。

### 自定义图源（custom_json）

选择 `custom_json` 后填写下面几项，即可接入任何返回 JSON 的图源：

| 配置项 | 示例 | 说明 |
| --- | --- | --- |
| 接口地址 | `https://api.example.com/search?q={query}&n={count}` | 支持 `{query}` `{count}` `{page}` 占位符；没有占位符时会自动拼 `?q=` |
| 请求头 | `{"X-Api-Key":"xxx"}` | JSON 格式，用于需要 Key 的图源 |
| Referer | `https://example.com` | 部分图源校验来源页 |
| 结果数组路径 | `data.items` | 点号路径；留空表示根节点就是数组 |
| 图片地址字段 | `url,thumb` | 按优先级排列，支持点号路径 |
| 标题字段 | `title` | 用于安全过滤和相关性排序 |
| 来源页字段 | `link` | 可选 |

**例子**：用自定义方式接入 360 图片（等价于内置图源）

```
接口地址      https://image.so.com/j?q={query}&pn=0&rn={count}&src=srp
Referer       https://image.so.com/
结果数组路径  list
图片地址字段  thumb,img
标题字段      title
来源页字段    link
```

**例子**：接入需要 Key 的图源（以 Pixabay 为例）

```
接口地址      https://pixabay.com/api/?key=YOUR_KEY&q={query}&per_page={count}&image_type=photo
结果数组路径  hits
图片地址字段  webformatURL,largeImageURL
标题字段      tags
来源页字段    pageURL
```

## 工作原理

```
用户: "搜一张布偶猫的图片"
  ↓
LLM 调用 search_image(query="布偶猫", count=2)
  ↓
插件请求图源 → 解析 JSON → 过滤/排序 → 选出直链
  ↓
插件下载图片（UA/Referer/超时可控）+ 校验文件头（JPEG/PNG/GIF/WEBP）+ 检查大小
  ↓
以本地文件组成 MessageChain 发送
  ↓
返回结果文本给模型，模型据此回复
```

图片下载到 `data/temp/image_search/`，由 AstrBot 自带的临时目录清理器定期回收。

## 已知限制

- 内置图源是**公开接口**，可能随图源调整而失效；自定义图源可以随时替换
- 相关性依赖图源本身的排序，插件只做"标题含关键词优先"的轻量重排
- 英文关键词在中文图源上召回差，建议用中文
- 部分图源有防盗链，若大量下载失败可以配置 `Referer`

## 兼容性

- AstrBot >= 4.17.0
- 平台：任意（Telegram / QQ / 微信 / 飞书等，只要支持发送图片组件）

## License

MIT
