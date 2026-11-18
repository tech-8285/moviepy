# 字幕可见性与时间归属分析

分析对象：本仓库 MoviePy（`pyproject.toml` 声明 `version = "2.2.0"`，`moviepy/version.py` 回退值 `2.1.2`），提交 `211e4b1`。

分析范围：`moviepy/video/tools/subtitles.py`、`moviepy/video/compositing/CompositeVideoClip.py`、`moviepy/video/VideoClip.py`、`moviepy/Clip.py`、`moviepy/video/io/ffmpeg_writer.py`、`moviepy/video/io/ffmpeg_reader.py`。

证据标记约定：

- **[源码]**：只由源码可直接读出的事实。
- **[内存帧]**：在本机用真实 MoviePy 对象 `get_frame()` 取帧、逐像素验证过。
- **[编码媒体]**：实际写盘（`write_videofile`）后用 ffmpeg 解码逐帧判读。
- **[待媒体证据]**：仅由源码或内存帧推断，仍需针对具体编码器/容器做输出判读才能定论。

---

## 0. 结论摘要

1. **一个 `SubtitlesClip` 每帧最多只能选出一条字幕条目**。选择逻辑是“缓存优先、列表顺序兜底”，然后取匹配列表的第 0 个（`subtitles.py:94-107`）。因此在同一个 `SubtitlesClip` 内，重叠条目**不会叠字**，只会二选一。要同时显示两条，必须建两个 `SubtitlesClip`（各自一个），再放进同一个合成（**已验证**：两条同时出现）。
2. **选择结果依赖“渲染历史”**，而不仅是条目时间表和列表顺序：`self.textclips` 是跨所有拷贝共享的插入序字典；构造期若有一条覆盖 `t=0` 的条目，它会在**构造阶段**被生成并缓存；此后任何一次取帧都会把命中的条目写进缓存。缓存命中的条目会**优先于** `self.subtitles` 的列表顺序（`subtitles.py:94-104`）。
3. **时间归属分三层，并且会叠加偏移**：
   - 条目时间（SRT 里的绝对秒数）只用于 `text_start <= t < text_end` 的判定；
   - `subclipped(a,b)` 会把内容按 `t -> t + a` 重定基（`Clip.py:428`），同时把 `duration` 改成 `b-a`、`end` 改成 `start + duration`（`Clip.py:455-456`；`SubtitlesClip.start` 恒为 0，故即 `b-a`）；
   - `with_start(s)` **只改播放窗口**（`Clip.py:246-250`），内容查找时间在 `compose_on` 里再减一次 `start`（`VideoClip.py:755`），于是条目会出现在“原时间 + s”，即**双重偏移**。
4. **“文字存在但不可见”有多种成因**：无命中条目（占位图）、命中但文字贴图在该查找时间的 mask 为 0（如 `with_opacity(0)`、动态/带淡入的生成器）、被上层不透明图层遮挡、被放到画布外、文字颜色等于背景色、条目短于一个帧周期而从不被采样。
5. **半开区间**：每帧的包含判定是 `start <= t < end`，所以 `end` 那一帧不显示；`iter_frames` 采样 `t = k / fps`（`Clip.py:558-563`），因此“可见的最后一帧”是严格小于 `end` 的最大网格点。
6. **“输出有没有 alpha”由背景决定**：默认 `CompositeVideoClip` 用不透明黑底（`CompositeVideoClip.py:82`、`87-91`），且背景 clip 无 mask 时 `comp.mask is None`，字幕 alpha 已在 `compose_on` 里烘焙进 RGB（`.mp4` 的 h264 → `yuv420p` 本来也不带 alpha）。只有 `bg_color=None`（或用带 mask 的背景 clip）时才存在独立 alpha；此时要保留它必须选支持 alpha 的编码器/容器（本机 `.mov` + `qtrle`/`prores_ks` 可以，h264、libvpx(vp8) 不行）。**[源码 + 编码媒体]**
7. **生成器契约比文档更严**：构造期会用 `"T"` 调一次生成器（`subtitles.py:122`），且要求所有贴图的 mask 存在性一致；否则要么 `KeyError`，要么渲染时 `AttributeError`。若生成器返回的贴图 mask 与图像不同尺寸，视频帧与导出 alpha 的几何会不一致（§3.4）。

---

## 1. `SubtitlesClip` 的条目模型与选择顺序

### 1.1 数据与初始化

`SubtitlesClip.__init__`（`moviepy/video/tools/subtitles.py:56-123`）：

- `VideoClip.__init__(self, has_constant_size=False)`（`:57`）——注意**没有 `size` 属性**，`hasattr(sc, "size") == False` **[内存帧]**。
- 输入可以是 SRT 路径或 `[((start, end), text), ...]`；只有 `list` 会原样使用（`:59-61`）。
- `self.duration = max([tb for ((ta, tb), txt) in self.subtitles])`（`:86`），`start = 0`、`end = duration`（`:85-87`）。
  - **空列表**会抛 `ValueError: max() iterable argument is empty` **[内存帧]**。
  - 时长为 0 的条目 `((4.0, 4.0), "A")` 会让 `duration = 4.0` 但永远不会被选中（`start <= t < end` 恒假）**[内存帧]**。

### 1.2 选择函数

```python
# moviepy/video/tools/subtitles.py:89-111
def add_textclip_if_none(t):
    sub = [
        ((text_start, text_end), text)
        for ((text_start, text_end), text) in self.textclips.keys()   # ① 先查缓存
        if (text_start <= t < text_end)
    ]
    if not sub:
        sub = [
            ((text_start, text_end), text)
            for ((text_start, text_end), text) in self.subtitles       # ② 再查原始列表
            if (text_start <= t < text_end)
        ]
        if not sub:
            return False
    sub = sub[0]                                                        # ③ 永远取第一个
    if sub not in self.textclips.keys():
        self.textclips[sub] = self.make_textclip(sub[1])
    return sub
```

由此得到 4 条硬事实：

| 事实 | 依据 | 证据 |
|---|---|---|
| 同一 `SubtitlesClip` 每帧最多一个条目 | `sub[0]` | **[源码]** + **[内存帧]**（重叠 2..4 只出现一条文字） |
| 缓存命中优先于原始列表顺序 | 先遍历 `self.textclips` | **[内存帧]**（见 §1.3、§4.2） |
| 缓存是插入序 `dict`，命中条目按“首次被选中的先后”排序 | `dict` 语义 + 只在 `sub[0]` 处插入 | **[源码]** |
| 选择具有副作用（生成文字贴图并写缓存） | `:108-109` | **[源码]** |

### 1.3 缓存是跨拷贝共享的（关键）

`VideoClip.__copy__`（`moviepy/video/VideoClip.py:145-165`）是**浅拷贝 `__dict__`**，只对 `mask`、`audio` 再做一次浅拷贝；`textclips` 不在特判名单里，于是：

```python
sc.textclips is sc.with_position(...).textclips      # True   [内存帧]
sc.textclips is sc.subclipped(a, b).textclips        # True   [内存帧]
sc.textclips is sc.with_opacity(0.5).textclips       # True   [内存帧]
```

**后果**：先在某个派生/剪裁视图上取过帧，会改变“原对象”之后渲染时的选择结果。这不是并行安全问题，而是**渲染顺序决定输出内容**。

### 1.4 构造期就会发生一次“取帧”

`self.mask = VideoClip(make_mask_frame, is_mask=True)`（`subtitles.py:123`）在 `VideoClip.__init__` 里会执行：

```python
# moviepy/video/VideoClip.py:114-116
if frame_function:
    self.frame_function = frame_function
    self.size = self.get_frame(0).shape[:2][::-1]
```

即构造时立刻调用 `make_mask_frame(0)`。若**有任意条目覆盖 `t=0`**，该条目会在构造阶段被生成并写进 `self.textclips`；否则缓存为空。

实测：

| 条目表 | 构造后缓存 | `mask.size` |
|---|---|---|
| `[((0,1),"A"), ((2,3),"B")]` | `[(0.0,1.0)]` | `(W,H)`（取到真实帧） |
| `[((1,2),"A")]` | `[]` | `(1,1)`（占位帧） |

**[内存帧]**

因此 `mask.size` / `mask` 的“声明尺寸”本身就是**是否有条目覆盖 `t=0`** 的函数。实际合成时 `compose_on` 会按图像帧尺寸裁剪/补零 mask（`VideoClip.py:766-782`），所以通常不出错；但 `mask.size` 不可当作“字幕尺寸”依赖（`with_mask("auto")` 的常量尺寸分支 `VideoClip.py:1063-1064` 也读它，只是 `SubtitlesClip.has_constant_size` 为 `False`，不会走那条分支）。

### 1.5 `hasmask` 探针与生成器契约

```python
# moviepy/video/tools/subtitles.py:122-123
hasmask = bool(self.make_textclip("T").mask)
self.mask = VideoClip(make_mask_frame, is_mask=True) if hasmask else None
```

- 构造时会**用文本 `"T"` 调用一次用户生成器**，结果除“是否有 mask”外全部丢弃。
- 生成器必须能接受任意文本、并且**每次都返回同一类（有/无 mask）的 clip**：
  - 只接受已知文本的生成器 → `KeyError: 'T'` **[内存帧]**。
  - “T”探针有 mask、其他条目无 mask → 选中该条目时 `make_mask_frame` 执行 `self.textclips[sub].mask.get_frame(t)`，`mask is None` → `AttributeError: 'NoneType' object has no attribute 'get_frame'` **[内存帧]**。
  - “T”探针无 mask → `self.mask is None`，之后即使其他条目有 mask 也不会被当作字幕掩膜使用。

### 1.6 无命中时的占位帧

```python
# moviepy/video/tools/subtitles.py:113-119
def frame_function(t):
    sub = add_textclip_if_none(t)
    return self.textclips[sub].get_frame(t) if sub else np.array([[[0, 0, 0]]])   # 1x1 黑

def make_mask_frame(t):
    sub = add_textclip_if_none(t)
    return self.textclips[sub].mask.get_frame(t) if sub else np.array([[0]])      # 1x1 透明
```

- 有 mask 时：占位帧 alpha=0，合成后**不可见**（**已用编码媒体与内存帧确认**）。
- 无 mask（生成器返回 `transparent=False` 或不带 alpha 的 clip）时：这个 1×1 黑色像素会被**实心粘贴**到 `pos` 位置，每个无字幕帧都留下一个黑点 **[内存帧]**；它在编码后是否仍可见取决于编码器/码率 **[待媒体证据]**（本机 x264 默认参数的一次 8fps 测试中黑点被抹掉了）。

---

## 2. 时间归属：谁的时间，传给谁

### 2.1 三个时间概念

| 时间 | 含义 | 产生位置 |
|---|---|---|
| 合成时间 `t` | `CompositeVideoClip.frame_function` 收到的时刻 | `CompositeVideoClip.py:156` |
| 剪辑本地时间 `ct = t - clip.start` | 传给字幕对象的时间 | `VideoClip.py:755`、`:919` |
| 条目判定时间 | 与 `(text_start, text_end)` 比较的时间 | `subtitles.py:97,103` |

关键点：**`SubtitlesClip` 没有任何时间重映射**。`frame_function(t)` 把同一个 `t` 既用于条目判定、又用于 `textclip.get_frame(t)`（`subtitles.py:113-119`）。所以“条目时间”同时充当了“文字贴图的内部时间”。

### 2.2 `subclipped` 会重定基内容

```python
# moviepy/Clip.py:428
new_clip = self.time_transform(lambda t: t + start_time, apply_to=[])
...
new_clip.duration = end_time - start_time    # :455
new_clip.end = new_clip.start + new_clip.duration   # :456
```

因为 `ImageClip.time_transform` 不做时间变换（`VideoClip.py:1497-1506` 只处理 mask/audio），而 `SubtitlesClip` 是普通 `VideoClip`，`time_transform` 走 `Clip.time_transform`（`Clip.py:151-158`）→ `transform`（`Clip.py:132`）→ `with_updated_frame_function`，于是新对象的 `frame_function(t)` 实际查找 `t + start_time`。

**效果**：`sc.subclipped(2, 4)` 的本地 `0..2` 对应源时间 `2..4`，条目 `[1,4)` 在本地 `0..2` 全部可见，本地 `2.0` 起消失 **[编码媒体]**。

### 2.3 `with_start` 只移动播放窗口，不重定基内容

```python
# moviepy/Clip.py:246-250
self.start = t
if (self.duration is not None) and change_end:
    self.end = t + self.duration
```

内容查找在 `compose_on` 里做 `ct = t - self.start`（`VideoClip.py:755`），再交给 `SubtitlesClip.frame_function`，此时仍与**原始条目时间**比较。

**效果（双重偏移）**：条目 `[1,2)` + `sc.with_start(3)`，在合成时间 `4.0..5.0` 才显示，而不是 `3.0..4.0` **[内存帧 + 编码媒体]**（实测 A 出现在 4.0/4.12/…/4.88）。

结论：`with_start` 只适合“整段时间线后移”的普通 clip；对 `SubtitlesClip` 做“从第 3 秒开始播字幕”的语义会多偏移一次。若要“截取一段并让条目时间随片段重排”，应在**数据层重定基**（见 §7.2），或者用 `subclipped`（它确实会重定基内容）。

### 2.4 文字贴图收到的是“字幕对象的本地时间”，不是“条目内相对时间”

延迟条目 `[5,7)` 的文字贴图会收到 `t = 5.0, 5.5, 6.5, …`，而不是 `0.0, 0.5, 1.5` **[内存帧，探针记录 get_frame 入参]**。

后果：

- 生成器返回**静态** `TextClip` → 无影响。
- 生成器返回带 `FadeIn(2)` 的 clip → 该效果以 `t=0` 为起点，`t>=2` 后恒为全不透明；对 `[5,7)` 的条目，淡入**永远看不出** **[内存帧：5.0/5.5/6.0 都是 255]**。
- 生成器返回只在 `t<1` 有内容的动画 clip → 对晚于 1 秒的条目**永远不可见**（文字存在、alpha 恒 0）**[内存帧：红色最大值恒 0]**。
- 带 `FadeOut` 的 clip 需要 `clip.duration`，字幕文字贴图默认 `duration=None` → `ValueError: Attribute 'duration' not set`。

### 2.5 `SubtitlesClip.duration` 与合成时长

- `SubtitlesClip.duration = max(end)`（`:86`），与 `start` 无关；`mask` 直到 `with_duration` 才拿到 `duration`（`Clip.py:290-316`）。`sc.mask.duration` 在构造后是 `None` **[内存帧]**。
- `CompositeVideoClip` 时长 = `max(clip.end for clip in self.clips)`（`CompositeVideoClip.py:118-123`），**不含 `bg`**。
  - 字幕最后一条结束于 8s、视频 6s → 合成 8s；6s 之后背景是黑（`bg` 为 `ColorClip`，视频不在播放窗口内），字幕照常显示 **[编码媒体：10fps 写出 80 帧，6.5s 处文字在、背景黑]**。
  - `use_bgclip=True` 时 `self.clips = clips[1:]`（`CompositeVideoClip.py:106-108`），时长只由**非背景** clip 决定：视频 10s、字幕结束 4s → 合成 4s，视频**被截断** **[编码媒体：写出 32 帧而不是 80 帧]**。注意这与“视频 2s、背景用 `ColorClip`”的用例对照：后者 `bg` 不被计入，字幕仍把合成拉到 4s。

---

## 3. 合成：选择顺序 × 裁切时间 × 掩膜

### 3.1 每帧的合成顺序

```python
# moviepy/video/compositing/CompositeVideoClip.py:115-116
self.clips = sorted(self.clips, key=lambda clip: clip.layer_index)
```

`sorted` 是稳定的：**同 layer 时保持传入列表顺序，后出现者在上层**（与类文档 `:25-29` 一致）。

```python
# :199-209
for clip in self.playing_clips(t):
    current_frame, current_mask = clip.compose_on(current_frame, t, current_mask)
    if self.mask and self.memoize_mask:
        self.mask.precomputed[t] = current_mask
```

`playing_clips`（`:211-215`）用**标量** `is_playing(t)`，即 `start <= t < end`（`Clip.py:598`）。

### 3.2 不透明背景下单层 alpha 的实际公式

```python
# moviepy/video/VideoClip.py:839-857（clip 有 mask、背景无 mask）
alpha = clip_mask[y1:y2, x1:x2][..., None]
result = frame * alpha + bg * (1 - alpha)     # :853
```

即标准 source-over，写回 uint8 RGB。**字幕的“透明度”在这里被烘焙成颜色**；一旦背景不透明，输出帧不再有独立 alpha。

两层都有 mask 时（透明合成，`:886-901`）：

```python
final_alpha = alpha_clip + alpha_bg * (1 - alpha_clip)                 # :895
result = (frame*alpha_clip + bg*alpha_bg*(1-alpha_clip)) / safe_alpha  # :897
```

掩膜单独合成（`compose_mask`，`:963-967`）：

```python
b[y1:y2, x1:x2] = clip_mask[...] + background_mask[...] * (1 - clip_mask[...])
```

### 3.3 合成器的两个“短路”会影响透明判断

```python
# VideoClip.py:804-810
if background_mask is not None and np.min(background_mask) == 1:
    background_mask = None
if clip_mask is not None and np.min(clip_mask) == 1:
    clip_mask = None
```

- **全 1 的 mask 被当作“无 mask”**（走直接粘贴分支 `:818-825`）。因此“mask 全为 1”和“mask 为 None”在合成结果上等价，但会走不同代码路径。
- 对 `TextClip`：`transparent=True`（默认）时 alpha 取自 Pillow 的 RGBA（`VideoClip.py:1443-1449`），字形外 alpha=0、字形内=1；`bg_color=...` 会把整图 alpha 填成 1（`ImageClip` 的 4 通道被拆成 mask，全 1）→ 走粘贴分支，**整块矩形不透明** **[内存帧]**；`transparent=False` 时没有 mask。

### 3.4 `SubtitlesClip` 的 mask 与 image 是否一致

`SubtitlesClip.mask` 是独立的 `VideoClip(make_mask_frame)`，它和 `frame_function` **各自**调用 `add_textclip_if_none(t)`。二者共享同一个缓存字典，所以：

- 同一 `t` 下选中同一条目 **[源码 + 内存帧]**；
- 选中的文字贴图的 `get_frame(t)` 与 `.mask.get_frame(t)` 被分别调用；
- **如果一个文字贴图的 mask 与图像尺寸不同**，两条路径的摆放规则不同：图像路径会把 mask 裁剪/补零到图像尺寸、左上对齐（`VideoClip.py:766-782`），而 `compose_mask` 不做任何缩放，直接按 mask 自身尺寸定位（`:928-967`）。

实测（透明合成，画布 60×20；图像 20×60 的红色块在 rows 5–9 / cols 5–19；mask 10×30 的白块在 rows 2–4 / cols 2–7）：

| 路径 | 结果 |
|---|---|
| 图像路径（`compose_on`） | 红色像素 **0 个**（该处 alpha 来自被补零的 mask，恰好为 0） |
| alpha 路径（`compose_mask`） | alpha>0.5 的 bbox = rows 2–4 / cols 2–7 |
| 对照组（mask 与图像同为 20×60、白块与红块对齐） | 两者 bbox 都是 rows 5–9 / cols 5–19 |

**[内存帧]** 也就是说：**alpha 里“有字幕”的区域，和视频帧里“看得到字”的区域可以完全不同**；极端情况下 alpha 声称存在、画面却完全没有内容（另一种“文字存在但不可见”）。普通 `TextClip` 的 mask 与图像同尺寸，不会触发；自定义生成器（尤其返回缩放过的 mask、或先 `image_transform` 再 `with_mask`）才会。

---

## 4. 判定表：文字存在但不可见 / 显示了另一条文字

### 4.1 “文字存在但不可见”的条件

“存在”指条目被选中并已生成文字贴图（缓存在 `textclips` 中）。“不可见”指最终像素与背景无可区分差异。

| # | 条件 | 机制 | 证据 |
|---|---|---|---|
| V1 | 该时刻无条目匹配 | 返回 1×1 占位；有 mask 时 alpha=0 | **[源码]** `subtitles.py:113-119` |
| V2 | 有 mask，但 `with_opacity(0)` | `mask.image_transform(lambda pic: 0*pic)`（`VideoClip.py:1086`）→ alpha 全 0 | **[内存帧 + 编码媒体]**：红像素 0 |
| V3 | 生成器返回动态贴图，且该贴图在**字幕本地时间** alpha=0 | 传入的是 `t`（绝对），不是条目内相对时间 | **[内存帧]**：`[5,7)` 条目 + `t<1` 才亮的贴图 → 恒不可见 |
| V4 | 生成器用 `FadeIn` 等以 0 为起点的效果 | 同上，对晚条目效果已“结束” | **[内存帧]**：`FadeIn(2)` 在 5.0/5.5/6.0 均为 255 |
| V5 | 被更高层不透明 clip 覆盖 | `layer_index` 排序后逐层 source-over | **[内存帧 + 编码媒体]**：字幕列在前、layer 0，被不透明绿块完全盖住 |
| V6 | 位置完全在画布外 | 目标区域裁剪后为空，不写入任何像素 | **[内存帧]** 64×32 画布上 `(-64,0)`、`(0,-24)`、`(63,0)`、`(64,0)` 均 0 像素（注意 `(0,-20)` 仍有 4 像素：字块并未完全出画） |
| V7 | 文字颜色 == 背景颜色 | alpha=1，但 RGB 与背景相同 | **[内存帧]**：黑字黑底，`comp.mask is None`、帧内全 0 |
| V8 | 条目短于一个帧周期，且与采样网格不重叠 | `iter_frames` 只采样 `k/fps`（`Clip.py:558-563`） | **[编码媒体]**：`[4.01, 4.02)` 在 24fps 下 0 帧可见 |
| V9 | 条目 `start == end` | `start <= t < end` 恒假 | **[内存帧]** |
| V10 | 无 mask 生成器 + 无字幕帧 | 1×1 黑色占位被实心粘贴 | **[内存帧]**：`pos` 处 1 个黑像素；编码后是否可见 **[待媒体证据]** |
| V11 | `pos` 落在“画布外 1–2 倍尺寸”的窄带 | 目标区域切片长度为 0，而源区域用负索引切出非空片段，两者长度不等 | **[内存帧]**：64×32 画布上，`x ∈ [65,126]` 或 `x ∈ [-127,-65]`、`y ∈ [33,62]` 或 `y ∈ [-63,-33]` 抛 `ValueError: operands could not be broadcast...`（`VideoClip.py:853`）；更远处（abs(x) ≥ 128、abs(y) ≥ 64）两侧都为空，反而正常 |
| V12 | 生成器给的贴图 mask 尺寸 ≠ 图像尺寸 | 图像路径按图像尺寸对齐/补零，alpha 路径按 mask 尺寸对齐 | **[内存帧]**：alpha bbox (2–4, 2–7)，画面红像素 0（§3.4） |

### 4.2 “显示了另一条文字”的条件

只要 `t` 同时落在两条或多条条目内，就会发生“二选一”，胜者由**缓存状态**和**列表顺序**共同决定。

设 `S(t) = { cue | cue.start <= t < cue.end }`，`|S(t)| >= 2`：

1. **缓存中有命中条目** → 取**最先被插入缓存**的那条（`textclips` 的插入序）。
2. **缓存中无命中条目** → 取 `self.subtitles` 中**最先出现**的那条。
3. **构造期种子**：若某条目覆盖 `t=0`，它在构造时就被插入缓存，于是它在**任何**重叠区间都优先于列表更靠前的其他条目。
4. **渲染历史**：任何一次对共享缓存对象（原对象、`with_position`、`with_opacity`、`subclipped`、`with_start` 的派生对象）的取帧，都可能把某条条目提前写入缓存。

实测矩阵（条目 `A=[1,4)`, `B=[2,5)`，观察 2.0–4.0）：

| 场景 | 2.0–3.9 显示 | 4.0–5.0 显示 |
|---|---|---|
| 全新对象，列表 `[A, B]`，从 0 开始顺序渲染 | **A** | B |
| 全新对象，列表 `[B, A]`，从 0 开始顺序渲染 | **A**（顺序渲染先经过 A 的独占区 `[1,2)`，A 先入缓存） | B |
| 先对 `sc.subclipped(4.2,5.0)` 取一帧（缓存 B），再整体渲染 | **B** | B |
| 先取 `sc.get_frame(3.0)`（缓存 A），再整体渲染 | A | B |
| 整体渲染 vs `subclipped(2.5,5.0)`，列表 `[A(2,5), B(1,4)]`（列表序与时间序相反） | 整体从 0 顺序渲染时先经过 B 的独占区 `[1,2)`，B 先入缓存 → **B**；子片段从 2.5 起渲染，首次取帧已在重叠区，缓存为空 → 按列表序取 **A** | — |
| 列表 `[A(1,4), B(0,2)]`（B 覆盖 0，构造期种子化） | 1.0–1.9 **B**；2.0 起 A | — |

均 **[内存帧 + 编码媒体]**。

> 特别提醒：表中第 5 行说明**“同一段源时间，完整渲染和剪裁后渲染会得到不同文字”**——这正是“裁切后的播放时间”与“选择顺序”共同作用的最直接表现。

---

## 5. 端点与采样边界

### 5.1 条目端点

- 包含判定：`text_start <= t < text_end`（`subtitles.py:97,103`）——**左闭右开**。
- `is_playing` 标量版同样左闭右开（`Clip.py:598`），数组版却用 `t <= self.end`（`Clip.py:592-594`）——**两版不一致**：对 `[1,4)`，`is_playing(np.array([3.9, 4.0]))` 返回 `[1, 1]`（把 `4.0` 算作在播），而标量 `is_playing(4.0)` 返回 `False` **[内存帧]**（数组版仅在 `t.min() >= end` 时才整体提前返回 `False`）。合成路径用的是标量版（`CompositeVideoClip.py:215`），所以当前不影响成片；但任何自己用数组版做判定/统计的代码会多算一帧。
- 相邻条目 `[1,2)` + `[2,3)`：`t=1.999` 是 A，`t=2.0` 是 B，`t=3.0` 无。**[内存帧]**
- 条目 `end` 恰为帧时间时会消失：`[1.0, 1.5)` 在 24fps 下最后可见帧是 `1.4583`，`1.5` 的帧为空。**[编码媒体]**

### 5.2 帧采样

```python
# moviepy/Clip.py:558-563
frame_index = np.arange(0, int(self.duration * fps))
t = frame_index / fps
```

- 采样时刻是 `0, 1/fps, 2/fps, ...`，**不含** `duration` 本身。
- 可见帧数 = 满足 `k/fps ∈ [start, end)` 的整数 `k` 的个数 = `ceil(end*fps) - ceil(start*fps)`（受浮点影响）。例：`[1.0, 1.5)` @24fps → `ceil(36) - ceil(24) = 12` 帧，实测 12 帧。
- 当 `[start, end)` 整段落在两个网格点之间时（如 `[4.01, 4.02)` @24fps → `96.24..96.48`，`ceil` 相减为 0），**零帧可见**（V8）。
- 浮点：很多常见端点能精确落在网格上（24fps 的 1.5/2.0/3.0；30fps 的 1.5/2.0/3.0），但 25fps 的 1.5、29.97fps 的多数端点不能 **[内存帧]**。因此“条目端点是否恰好是一帧”不是稳定性质。

### 5.3 剪辑端点附近的 API 行为

| API | 行为 | 证据 |
|---|---|---|
| `subclipped(a, b)` | 内容重定基 `t+a`；`duration=b-a`；超出 `self.duration` 抛 `ValueError` | **[源码]** `Clip.py:428,448-456` |
| `with_start(s)` | 只改窗口；内容按 `t-s` 查找 → 对字幕产生双重偏移 | **[内存帧 + 编码媒体]** |
| `with_end(e)` | 设 `end` 与 `duration`，不重定基内容 | **[源码]** `Clip.py:277-284` |
| `with_duration(d)` | 只改 `start`/`end`/`duration`，不重定基内容；补出来的尾段没有条目匹配，仍走占位帧 | **[源码 + 内存帧]** |
| `with_section_cut_out(a, b)` | `time_transform` 跳段并同时作用于 mask/audio | **[源码]** `Clip.py:484-490` |
| `in_subclip(a, b)` | **会漏掉完整覆盖窗口的条目**：`(a <= t1 < b) or (a < t2 <= b)` | **[内存帧]**：`[0,10]` 在 `(3,4)` 窗口返回 `[]` |
| `in_subclip(None, x)` | 异常被 `except` 吞掉 → 返回 `[]` | **[内存帧]** |
| `match_expr(expr)` | 用过滤后的条目新建 `SubtitlesClip`，但**不传 `make_textclip` 也不传 `font`** → 无论原对象怎么构造，都会抛 `ValueError: Argument font is required if make_textclip is None.` | **[内存帧]** |
| `file_to_subtitles` | 只在**空行**处提交条目；文件末尾没有空行时**最后一条被丢弃** | **[内存帧]**：无尾随空行返回 `[]` |
| `file_to_subtitles` | 文件开头空行会插入 `(None, "")`，`SubtitlesClip` 随后 `TypeError: cannot unpack non-iterable NoneType object` | **[内存帧]** |
| `write_srt` | 用 `" - "` 分隔而不是 SRT 的 `-->`，输出不是合法 SRT | **[源码]** `subtitles.py:155-173` |

---

## 6. 现有实现可保证的边界

以下是在**不改代码**的前提下可以依赖的性质：

1. **每帧至多一条文字**：一个 `SubtitlesClip` 内不会叠字；重叠区间一定是“某一条”。
2. **条目区间左闭右开**：`start` 那一帧可见，`end` 那一帧不可见（以标量 `is_playing` / `add_textclip_if_none` 为准）。
3. **时间判定与文字贴图取帧使用同一个数值** `t`：`frame_function` 与 `make_mask_frame` 传下去的都是 `t`；合成器再统一减去 `clip.start`。因此“选中的是哪条”和“取到的是哪张图/哪张 mask”在**时间**上是自洽的（几何上还要求贴图的 mask 与图像同尺寸，见 §3.4）。
4. **`subclipped` 后内容与窗口同步重定基**：`subclipped(a,b)` 的本地 `0..b-a` 恰好覆盖源 `a..b`；条目在该窗口内按源时间判定。
5. **同 layer 时列表顺序决定叠放**：稳定排序，后出现者在上。
6. **有 mask 时的无字幕帧是透明的**（1×1 alpha=0 被补零放大）。
7. **合成时长是“所有非背景 clip 的最大 end”**，不是视频时长。
8. **默认合成（`bg_color` 非 `None`，默认黑；背景 clip 自身无 mask）时输出没有 alpha**：`comp.mask is None`，字幕 alpha 已烘焙进 RGB，`.mp4` 输出不会出现“半透明”通道。若背景 clip 自带 mask（如 `bg_color=None` 或视频有 alpha），则 `comp.mask` 存在，见 §3.3、§7.3。

以下**不能**保证：

- 重叠条目的胜者不随渲染顺序变化（§1.3、§4.2）。
- `with_start` 之后条目仍对齐到“原时间 + start”（实际双重偏移）。
- 生成器只被以真实字幕文本调用（构造期有 `"T"` 探针）。
- 生成器返回的贴图尺寸随条目变化时，图像与 alpha 的几何一致。
- 短于 1/fps 的条目会被输出。
- `in_subclip` 的返回值是“窗口内全部条目”（会漏掉全覆盖条目）。
- `SubtitlesClip` 有 `size` 属性（没有）。

---

## 7. 建议的兼容处理

下面按“无需改库 → 小封装 → 何时才值得改库”排列。所有封装都只使用公开 API。

### 7.1 稳定选择：不要依赖缓存顺序

**首选做法**：建 `SubtitlesClip` 之前先把重叠消解掉，让任意时刻至多一条匹配。这样无论缓存如何，选择都唯一。

```python
def resolve_overlaps(cues, mode="later_starts_wins"):
    """cues: [((start, end), text), ...]  -> 不重叠的条目表（左闭右开，末尾裁剪）"""
    cues = sorted(cues, key=lambda c: (c[0][0], c[0][1]))
    out = []
    for (a, b), text in cues:
        if out and a < out[-1][0][1]:
            pa, pb = out[-1][0]
            if mode == "later_starts_wins":
                out[-1] = ((pa, a), out[-1][1])      # 前一条被后一条截断
                if a >= b:
                    continue
            elif mode == "longer_wins":
                if (pb - pa) >= (b - a):
                    continue
                out.pop()
        out.append(((a, b), text))
    return out
```

若必须保留重叠语义并指定优先级，就**自己接管选择**，不要复用 `add_textclip_if_none`（它把选择和缓存绑死）。一个最小实现（已实测：无预渲染、先渲染尾部、先渲染中段，三者结果一致）：

```python
import numpy as np
from moviepy.video.VideoClip import VideoClip
from moviepy.video.tools.subtitles import SubtitlesClip

class DeterministicSubtitlesClip(SubtitlesClip):
    def __init__(self, subtitles, make_textclip,
                 priority=lambda cue: (cue[0][1], cue[0][0])):  # 默认：结束晚者优先
        VideoClip.__init__(self, has_constant_size=False)
        self.subtitles = list(subtitles)
        self.textclips = {}
        self.font = None
        self.make_textclip = make_textclip
        self.priority = priority
        self.start = 0
        self.duration = max(end for ((_, end), _) in self.subtitles)
        self.end = self.duration

        def select(t):
            matches = [c for c in self.subtitles if c[0][0] <= t < c[0][1]]
            if not matches:
                return None
            cue = max(matches, key=self.priority)      # 与缓存/渲染历史无关
            if cue not in self.textclips:
                self.textclips[cue] = self.make_textclip(cue[1])
            return cue

        def frame_function(t):
            cue = select(t)
            return self.textclips[cue].get_frame(t) if cue else np.zeros((1, 1, 3), np.uint8)

        def make_mask_frame(t):
            cue = select(t)
            if cue is None:
                return np.zeros((1, 1))          # 无字幕：完全透明（与库一致）
            textclip = self.textclips[cue]
            if textclip.mask is not None:
                return textclip.mask.get_frame(t)
            # 生成器给的贴图没有 mask：当作整块不透明，避免“文字存在但 alpha 恒 0”
            return np.ones(textclip.get_frame(t).shape[:2])

        self.frame_function = frame_function
        self.mask = VideoClip(make_mask_frame, is_mask=True)
```

与库的 `make_textclip("T")` 探针相比有两点差异，写进封装时要注意：

- **不再用 `"T"` 调用生成器**，所以生成器不会被以不存在的文本调用；但这也意味着“是否有 mask”改为**逐条目**判断，混用有/无 mask 的生成器不会再崩。
- 无 mask 的贴图被当作“整块不透明”（`np.ones`），而不是原库的 `mask is None` 分支；两者在默认黑底合成里结果相同，但前者在 `bg_color=None` 时会额外产出一块矩形 alpha。若不想要，就把上一行改成 `return np.zeros(textclip.get_frame(t).shape[:2])`。

注意这个封装仍然保留“文字贴图接收绝对时间”的行为（与库一致）；它只消除**选择顺序**的不确定性，不修 §2.4。

### 7.2 剪裁：重定基数据，而不是移动播放窗口

```python
def rebase_cues(cues, offset, new_duration=None):
    out = []
    for (a, b), text in cues:
        a2, b2 = a - offset, b - offset
        if b2 <= 0:
            continue
        if new_duration is not None and a2 >= new_duration:
            continue
        out.append(((max(a2, 0.0),
                     b2 if new_duration is None else min(b2, new_duration)), text))
    return out
```

- 用 `SubtitlesClip(rebase_cues(cues, cut_start, cut_len))` + `CompositeVideoClip(..., duration=cut_len)`，语义清楚、没有双重偏移。
- 也可以直接用 `sc.subclipped(a, b)`（内容会重定基，**实测正确**），但要记住它把 `start` 归零、`duration=b-a`，且 `b` 不能超过 `sc.duration`。
- **不要**用 `sc.with_start(s)` 来实现“从第 s 秒开始显示字幕”——它会额外偏移一次。

### 7.3 透明度：明确“烘焙”还是“保留 alpha”

| 目标 | 做法 | 结果 |
|---|---|---|
| 直接出 `.mp4`，字幕半透明 | 默认 `CompositeVideoClip([video, subs])`（`bg_color=(0,0,0)`，且 `video.mask is None`） | `comp.mask is None`，alpha 在 `compose_on` 里与视频混合后烘焙进 RGB；**可正常得到半透明观感** **[内存帧]**：黑底上 `with_opacity(0.5)` 的红值由 255 降到 127，`with_opacity(0)` 降到 0 |
| 需要独立 alpha（叠加到别处） | `bg_color=None`，并且用**保留 alpha 的容器/编码器**：`.mov` + `qtrle` 或 `prores_ks`（本机两者都解出 alpha 通道） | `.mov(qtrle)` → `argb`；`.mov(prores_ks)` → `yuva444p12le`；cue 外 alpha=0，cue 内 alpha>0 **[编码媒体]** |
| 只想要字幕图层 | 单独 `CompositeVideoClip([subs], size=..., bg_color=None).write_videofile("sub.mov", codec="qtrle")` | alpha 保留，便于二次合成 |

> `libvpx-vp9` 在本机的这条写盘路径上**没有产出可用文件**（0 字节，ffprobe 报 `EBML header parsing failed`）；透明 webm 需要 `libvpx` + `-pix_fmt yuva420p` 且编码器支持，本机 `libvpx`(vp8) 实测解出 `yuv420p`、alpha 全 255。因此**不要**把 webm 当作“一定保 alpha”的默认选择，需在目标环境单独验证 **[待媒体证据]**。

补充：`with_opacity(op)` 对 `SubtitlesClip` 是**逐像素乘 mask**（`VideoClip.py:1086`）；若原生成器没有 mask，`add_mask_if_none` 会造一个**整帧**mask（`VideoClip.py:1062-1071`），于是 `op` 作用于整块矩形（包括 `transparent=False` 时的背景），而不是只作用于字。实测 `op=0` 时整块消失，`op=0.5` 时文字与底色一起半透明。

### 7.4 端点与采样：让条目“至少占据一帧”

```python
def snap_cues(cues, fps, min_frames=1):
    frame = 1.0 / fps
    out = []
    for (a, b), text in cues:
        a = round(a * fps) / fps
        b = max(round(b * fps) / fps, a + min_frames * frame)
        out.append(((a, b), text))
    return out
```

- 把条目端点对齐到帧网格，可以避免“恰好落在 `end` 上”的歧义，也避免亚帧条目被完全跳过。
- 相邻条目保持 `prev.end == next.start` 即可无缝换字（左闭右开保证不叠帧）。
- 若最后一条 `end` 大于视频时长，合成会被拉长（§2.5）。需要保持视频长度时显式传 `duration=`，或用 `use_bgclip` 并接受“被最短内容截断”。

### 7.5 位置

- 只用画布内的 `pos`。完全出画时：刚好越界（`-W ≤ x ≤ W` 之类）不显示也不报错，但落在 **外扩 1–2 倍** 的窄带会抛 `ValueError`（`VideoClip.py:853` 广播失败）；实测 64×32 画布的出错区间是 `x ∈ [65,126] ∪ [-127,-65]`、`y ∈ [33,62] ∪ [-63,-33]` **[内存帧]**。
- 用 `("center", "bottom")` 这类相对位置，避免手算像素后越界。

### 7.6 生成器契约（若要继续用原 `SubtitlesClip`）

- 必须接受任意文本（包括 `"T"`），并对所有文本返回**同一类**贴图（都有 mask 或都无 mask）。
- 不要在生成器里做按文本取值的查表（`KeyError: 'T'`）。
- 需要淡入淡出时，**不要**把 `FadeIn/FadeOut` 套在生成器返回的贴图上（时间基准是绝对时间）；应把每条字幕做成独立 clip：`textclip.with_start(a).with_duration(b-a).with_effects([CrossFadeIn(d)])`，再和视频合成。这样效果的时间基准就是 `clip.start`。
- 需要动画（滚动、逐字）时，同样用“每条一个独立 clip + `with_start`”的模型，或自行在生成器里把绝对时间换算成 `t - cue_start`（需自行保存 cue_start）。

### 7.7 同时显示多条文字

库内做不到，但可以显式堆叠：

```python
layers = [CompositeVideoClip([video])]
for i, ((a, b), text) in enumerate(cues):
    tc = make_textclip(text).with_start(a).with_duration(b - a).with_position(("center", 40 + 40 * i))
    layers.append(tc)
final = CompositeVideoClip(layers, size=video.size)
```

每条对应一个独立 clip，`is_playing` 用各自的窗口，互不干扰，等于把“选择”下放给了合成器。

---

## 8. 证据分级

### 8.1 纯源码事实（可复核）

- 选择算法：缓存优先、`sub[0]`、插入序（`subtitles.py:89-111`）。
- 构造期 `"T"` 探针与 `get_frame(0)` 尺寸探测（`subtitles.py:122-123`；`VideoClip.py:114-116`）。
- `subclipped` 的 `t + start_time` 重定基（`Clip.py:428`）。
- `with_start` 只改窗口（`Clip.py:246-250`），`compose_on`/`compose_mask` 的 `ct = t - self.start`（`VideoClip.py:755,919`）。
- 合成顺序按 `layer_index` 稳定排序（`CompositeVideoClip.py:116`）。
- alpha 公式（`VideoClip.py:853,895,897,963-967`）。
- 合成时长 = 非背景 clip 的最大 `end`（`CompositeVideoClip.py:118-123`）。
- 写出时 mask 转 alpha 并叠成 RGBA（`ffmpeg_writer.py:289,309-313`）。
- `iter_frames` 的网格与帧数（`Clip.py:558-563`）。
- 标量/数组 `is_playing` 的端点不一致（`Clip.py:581-598`）。

### 8.2 已用内存帧验证

- 重叠条目“二选一”，且胜者随缓存/渲染历史变化（§4.2 全表）。
- 构造期种子：覆盖 `t=0` 的条目优先。
- `[1,4)` 条目 + `with_start(3)` → 合成时间 4.0–4.9 可见（双重偏移）。
- `subclipped(2,4)` 的本地 0–2 对应源 2–4。
- `with_opacity(0)` → alpha 0；`with_opacity(0.5)` → 255→127。
- 无 mask 生成器的 1×1 黑点（直接取帧可见）。
- `in_subclip` 漏掉全覆盖条目；`match_expr` 丢生成器；`file_to_subtitles` 尾行丢失/首行空行崩溃。
- 完全出画（`|x| ≥ W` 或 `|y| ≥ H` 之外）不显示；落在“外扩 1–2 倍”的窄带抛广播 `ValueError`（区间见 V11）。
- 文字颜色 == 背景颜色时 alpha=1 但画面全黑。
- 生成器给的 mask 与图像尺寸不一致时，alpha bbox 与画面内容 bbox 完全不同（§3.4 / V12）。
- `with_opacity(0.5)` 在默认不透明合成里输出红值 127；在 `bg_color=None` 合成里 `comp.mask` 的 alpha 为 0.5。

### 8.3 已用编码媒体验证

本机 ffmpeg 8.1（Apple clang 16.0.0），`libx264`、`qtrle`、`prores_ks`，容器 mp4 / mov / webm，逐帧解码判读：

- 重叠胜者随“先渲染尾部”而改变（同一 cue 表，编码输出从 A 变 B）。
- 完整渲染 vs `subclipped(2.5,5)` 在同一源时间段显示不同文字（B vs A）。
- `[1.0,1.5)` 在 24fps 下最后可见帧 1.4583s，1.5s 帧为空。
- `[4.01,4.02)` 在 24fps 下 0 帧可见；`[4.0,4.0+1/24)` 可见 1 帧。
- `with_opacity(0)` 输出中红色像素为 0。
- 字幕结束晚于视频：视频 2s、字幕到 4s → 写出 40 帧（10fps），2s 后背景为黑、字幕仍在（视频**不冻结**最后一帧）。
- `use_bgclip=True`：视频 10s、字幕到 4s → 只写 32 帧（8fps），视频被截断；视频 2s、字幕到 1.5s → 15 帧。
- `.mp4`（h264）解出 `yuv420p`、alpha 全 255；`.mov` 用 `qtrle` 解出 `argb`、用 `prores_ks` 解出 `yuva444p12le`，两者 cue 外 alpha=0；`.webm` 用 `libvpx`(vp8) 解出 `yuv420p`、alpha 全 255。

### 8.4 仍需媒体输出证据的结论

1. **1×1 黑点在具体编码参数下是否可见**：内存帧中确定存在（`pos` 处一个纯黑像素）；在本机 x264 默认的一例中编码后消失。结论取决于分辨率、CRF/preset、缩放与色度抽样，需要针对目标参数做输出判读。
2. **“文字颜色 == 背景色”在 4:2:0 色度抽样后的可辨识度**：alpha=1、RGB 相同可以保证“视觉上等同”，但边缘像素的 YCbCr 往返会引入微小差异；是否“绝对不可见”需对具体编码取样测量。
3. **两条独立 `SubtitlesClip` 半透明叠加的混合正确性**：本机已确认两条同时出现（各自 alpha 生效），但两层半透明叠加后的最终 alpha/RGB 是否符合预期，需要对透明容器（本机已验证 `.mov`+`qtrle`/`prores_ks`）解码后逐像素核对。
4. **不同 ffmpeg 构建/编码器的 alpha 行为**：本机 h264 丢 alpha、qtrle/prores_ks 保 alpha、libvpx(vp8) 丢 alpha；`libvpx-vp9` 在本机写盘路径上产出 0 字节文件。这些是**编码器能力**而非库的保证，需在目标环境的构建上复测。
5. **`FadeIn` 等效果在“绝对时间基准”下的观感**：已经证明淡入对晚条目“不生效”；具体成片观感（是否出现突然全亮）需要看实际输出片段。
6. **动态贴图（滚动/逐字）的时间基准**：已证明传入绝对时间；不同生成器实现的结果差异较大，需按生成器逐个输出验证。

---

## 9. 最小复现

以下片段只依赖 `moviepy` 本身，可直接在仓库根目录运行（**不要**在下载/解压出的不可信目录里执行解释器；把脚本放在单独目录，路径作为参数传入）。

```python
import numpy as np
from moviepy.video.VideoClip import TextClip, ColorClip
from moviepy.video.tools.subtitles import SubtitlesClip
from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip

COLORS = {"A": (255, 0, 0), "B": (0, 0, 255), "T": (255, 255, 255)}

def gen(txt):
    return TextClip(text=txt, font_size=16, color=COLORS.get(txt, (255, 255, 255)),
                    size=(64, 32), method="caption")

def which(frame):                       # 用颜色判定“这一帧显示的是哪条”
    r = int(frame[:, :, 0].astype(int).sum())
    b = int(frame[:, :, 2].astype(int).sum())
    return "-" if r + b == 0 else ("A" if r > b else "B")

cues = [((1.0, 4.0), "A"), ((2.0, 5.0), "B")]
bg = lambda: ColorClip((64, 32), color=(0, 0, 0)).with_duration(5)

# 1) 全新对象：A 在重叠区间胜出（列表顺序）
sc = SubtitlesClip(cues, make_textclip=gen)
c1 = CompositeVideoClip([bg(), sc.with_position((0, 0))], size=(64, 32))
print("fresh   ", [(t, which(c1.get_frame(t))) for t in (2.5, 3.5)])

# 2) 先渲染尾部，把 B 写进共享缓存，再看同一时刻
sc = SubtitlesClip(cues, make_textclip=gen)
sc.subclipped(4.2, 5.0).get_frame(0.5)     # 缓存 B
c2 = CompositeVideoClip([bg(), sc.with_position((0, 0))], size=(64, 32))
print("after tail", [(t, which(c2.get_frame(t))) for t in (2.5, 3.5)])

# 3) 构造期种子：B 覆盖 t=0
sc = SubtitlesClip([((1.0, 4.0), "A"), ((0.0, 2.0), "B")], make_textclip=gen)
c3 = CompositeVideoClip([bg(), sc.with_position((0, 0))], size=(64, 32))
print("seeded  ", [(t, which(c3.get_frame(t))) for t in (1.5, 2.5)])

# 4) with_start 的双重偏移
sc = SubtitlesClip([((1.0, 2.0), "A")], make_textclip=gen)
c4 = CompositeVideoClip([bg(), sc.with_start(3.0).with_position((0, 0))], size=(64, 32))
print("shifted ", [(t, which(c4.get_frame(t))) for t in (3.5, 4.5, 5.5)])
```

预期输出（本机实测）：

```
fresh    [(2.5, 'A'), (3.5, 'A')]
after tail [(2.5, 'B'), (3.5, 'B')]
seeded   [(1.5, 'B'), (2.5, 'A')]
shifted  [(3.5, '-'), (4.5, 'A'), (5.5, '-')]
```

---

## 10. 一页速查

| 问题 | 答案 |
|---|---|
| 一个 `SubtitlesClip` 能同时显示两条吗？ | 不能，每帧只取 `sub[0]`。 |
| 重叠时显示哪一条？ | 缓存中先插入者优先；否则列表中最先出现者。缓存受构造期 `t=0` 种子和渲染历史影响。 |
| 为什么同一段视频，剪一段单独渲染后文字不一样？ | `subclipped` 会重定基时间；若同时共享了缓存副本，选择也可能翻转。 |
| `with_start(3)` 是“从第 3 秒开始显示字幕”吗？ | 不是。条目会出现在原时间 + 3（双重偏移）。 |
| 结束时间那一刻还显示吗？ | 不显示，左闭右开。`is_playing` 标量与数组版在此不一致。 |
| 为什么文字“存在”却看不到？ | 无 mask 的贴图 alpha 可能为 0、被上层盖住、出画、颜色等于背景、或条目短于一帧。 |
| 为什么动画/淡入不生效？ | 文字贴图收到的是字幕对象的绝对本地时间，不是条目内相对时间。 |
| 输出 `.mp4` 会有透明通道吗？ | 默认黑底合成时本来就没有 alpha；`bg_color=None` 时 h264 也会丢 alpha（`yuv420p`）。 |
| 字幕结束晚于视频会怎样？ | 合成被拉长，视频结束后背景为黑，字幕继续显示；`use_bgclip` 则相反，会被最短内容截断。 |
| 最省事的安全用法？ | 先把条目去重叠并对齐帧网格，用默认不透明合成输出 mp4；需要保留 alpha 时用 `.mov`+`qtrle`。 |
