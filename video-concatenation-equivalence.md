# 视频拼接等价性分析：`chain` 直连 vs `compose` 统一画布

分析对象：本仓库 `moviepy`，分支 `task/48`，HEAD `211e4b1`。
环境：Python 3.12.4；编码用 imageio-ffmpeg 9.0.2（`FFMPEG_BINARY`），探测用 `/usr/local/bin/ffprobe|ffmpeg`。

标记约定：

- **【源码】** 可由仓库源码行号直接确认。
- **【测试】** 仓库现有测试的覆盖情况（含"只 smoke、不断言内容"）。
- **【实验】** 本次在本地运行、仓库内**没有**对应测试或黄金文件的观察。
- **【缺口】** 需要媒体输出证据才能定论，而仓库中不存在的内容。

核心判断：**两种方式对"某个 clip 在局部时间 τ 取哪一帧"的规则完全相同；分歧只在"某一时刻由哪些 clip 参与、按什么顺序和透明度叠放"，以及 `chain` 分支的遮罩/尺寸管线。** 因此"输出帧率相同"推不出"显示内容相同"。

---

## 0. 结论速览

1. **时间安排与 method 无关。** 两种方式在同一函数里用同一组 `timings`，音频也用同一组起点构造。切 `method` 不改变总时长、分段起点、音频起点。
2. **`chain` 不重叠，它只是提前切走。** 负 `padding` 在 `chain` 下把下一段起点提前，上一段尾部直接不播；只有 `compose` 才让两段同时在场并合成。
3. **`chain` 的选择器只看起点、不看终点。** 它取"起点 ≤ t 的最大下标"；`compose` 用 `is_playing`（起点 ≤ t < 终点）。`chain` 的下标只增不减，`compose` 的参与集合随 `end` 收缩：只要有间隙、钳位或"顶层段先结束"，两者就选出不同素材，即使总时长完全相同。
4. **`chain` 的遮罩按零 padding 重建。** 视频用带 padding 的 timings，遮罩却用默认 `padding=0`，任何非零 padding 都会让遮罩与画面错位，遮罩时长也可与结果时长不同。
5. **`chain` 的 `size` 取自 `get_frame(0)` 的形状**，不是最大画幅。混画幅时会把错误尺寸交给 ffmpeg，产生静默截断而非报错。
6. **音轨对 method 可替换，但音轨与画面的对应不可替换**：重叠区 `chain` 只显示后一段画面，音频却是两段相加。
7. **像素等价不蕴含码流等价。** `compose` 默认总带遮罩，于是 webm 走 `yuva420p`、`chain` 走 `yuv420p`；内容相同的两段在 mp4 上 md5 可以相同，在 webm 上则不同。
8. 现有测试全部是 `padding=0`、同尺寸、时长/fps 断言或"能写出来"，**没有任何一条比较两种 method 的实际像素或 PCM**。

---

## 1. 两条分支

`concatenate_videoclips` 在 `moviepy/video/compositing/CompositeVideoClip.py:298-410`。两条分支共用前半段：

```python
timings = np.cumsum([0] + [clip.duration for clip in clips])          # :352
...
timings = np.maximum(0, timings + padding * np.arange(len(timings)))  # :359
timings[-1] -= padding                                                # :360

if method == "chain":
    def frame_function(t):
        i = max([i for i, e in enumerate(timings) if e <= t])         # :365
        return clips[i].get_frame(t - timings[i])                     # :366
    result = VideoClip(is_mask=is_mask, frame_function=frame_function) # :374
    if any([clip.mask is not None for clip in clips]):
        ...
        result.mask = concatenate_videoclips(masks, method="chain", is_mask=True)  # :377

elif method == "compose":
    result = CompositeVideoClip(
        [clip.with_start(t).with_position("center")
         for (clip, t) in zip(clips, timings)],                        # :380-384
        size=(w, h), bg_color=bg_color, is_mask=is_mask)
```

收尾代码对两条分支一致：`result.start, result.duration, result.end = 0, timings[-1], timings[-1]`（`:398`），音频 `CompositeAudioClip([a.with_start(t) ...]).with_duration(result.duration)`（`:404-406`）。

**【源码】** 因此：

- `method` 不影响 `timings`、`duration`、`start_times`、`fps`、音频起点。
- `chain` 的结果是 `VideoClip`（**不是** `CompositeVideoClip`）；`compose` 才是。
- 文档 `:339-344` 称 "padding 只用于 compose" 且 "非零 padding 自动切换为 compose"，但代码**没有自动切换**，`:359` 对两分支一视同仁。默认调用 `concatenate_videoclips([...], padding=-1)`（如 `moviepy/video/fx/SlideIn.py:39` 的示例）走的是 `chain`，并没有合成。
- `docs/user_guide/compositing.rst:58` 称 `concatenate_videoclips` 内部创建 `CompositeVideoClip`，与 `:374` 不符（`chain` 分支不是）。

---

## 2. 时间安排：公式与两个不同的钳位

**【源码】** 记 `d_i` 为时长、`n = len(clips)`、`C_i = Σ_{j<i} d_j`：

- 起点（i = 0..n，末项是哨兵）：`s_i = max(0, C_i + i·p)`（`:352`、`:359`）。
- 总时长：`D = s_n − p = max(0, Σd + n·p) − p`（`:360`）。

由此有**两个互不相同的阈值**，容易混淆：

| 现象 | 触发条件 | 结果 |
|---|---|---|
| 段起点被钳到 0 | 存在 i 使 `C_i + i·p < 0`（最小的是 `i=1`：`p < −d_0`） | 该段及后续段可能与前面同起点 |
| `D` 公式改变 | `Σd + n·p ≤ 0`，即 `p ≤ −Σd/n`（平均时长） | `D = −p`，**负得越多总时长越长** |

只要哨兵未被钳位，`D = Σd + (n−1)·p`（常见形式）。哨兵钳位后两者脱钩：`D` 变成 `−p`，与"Σd + (n−1)p" 无关。

对照 `(1,1)`：`p=−1` 时 `Σd + n·p = 0`，恰在阈值上，`D = −p = 1 = Σd+(n−1)p`，两个公式重合，所以单看这一组容易误以为公式总是后者。`p=−1.2` 才暴露差异（1.2 vs 0.8）。

非单调则完全是另一回事：**(10,1,10) 在 `p=−1.5` 时 `s_2 = 8 < s_1 = 8.5`，但 `Σd + n·p = 16.5 > 0`，哨兵未被钳位，`D` 仍按常规公式得 18**。可见"非单调"与"总时长公式改变"是两件独立的事。

**【实验】**

| clips | p | timings | D | Σd+(n−1)p |
|---|---|---|---|---|
| 1,1 | 0 | [0,1,2] | 2 | 2 |
| 1,1 | −0.5 | [0,0.5,1.5] | 1.5 | 1.5 |
| 1,1 | −1 | [0,0,1] | 1 | 1（恰在阈值上，两者重合） |
| 1,1 | −1.2 | [0,0,1.2] | **1.2** | 0.8 ✗ |
| 1,1 | −2 | [0,0,2] | **2** | 0 ✗ |
| 1,1,1 | −1 | [0,0,0,1] | **1** | 1（碰巧重合） |
| 10,1,10 | −1 | [0,9,9,19] | 19 | 19 |
| 10,1,10 | −1.5 | [0,8.5,8,18] | 18 | 18 ✓（非单调，但哨兵未钳位） |

注意表格最后一行：它说明**非单调不等于哨兵钳位**。起点倒序只要求 `d_1 + p < 0`（第二段的时长小于重叠量），此时后一段在时间上会落到前一段之前，而 `D` 仍按常规公式计算。这两件事互不蕴含。

**【源码】** `timings` 有 `n+1` 项，末项是哨兵。`zip(clips, timings)` 只取前 `n` 项（`:383`、`:401`），最后一段的起点是 `timings[n−1] = D − d_{n-1}`（未钳位时）。

---

## 3. 逐帧采样：两种选择规则

### 3.1 单个 clip 内部的取帧完全一致

**【源码】**

- `chain`：`clips[i].get_frame(t - timings[i])`（`:366`）。
- `compose`：`CompositeVideoClip.frame_function` 对每个 `playing_clips(t)` 调 `clip.compose_on(...)`，内部 `ct = t - self.start`（`moviepy/video/VideoClip.py:755`）再 `get_frame(ct)`（`:758`）。

局部时间都是"绝对时间 − 该段起点"，传给同一个 `clip.get_frame`。**帧率差异不在这层制造分歧**：`VideoFileClip` 用自己的 fps 量化 `int(fps*t + 0.00001)`（`moviepy/video/io/ffmpeg_reader.py:271-277`），`BitmapClip` 用 `int(t*fps)` 索引（`moviepy/video/VideoClip.py:2211`）；相同的 `t` → 相同的源帧。

`result.fps = max(fpss)`（`:408-409`；`CompositeVideoClip.__init__` `:93-94` 同值）只决定 `iter_frames`/`write_videofile` 的采样网格（`moviepy/Clip.py:516-571`），不改上述规则。

### 3.2 选择规则：只看起点 vs 起点+终点

**【源码】**

- `chain`：`i = max([i for i, e in enumerate(timings) if e <= t])`（`:365`）。候选集只由**起点**构成，**完全不看 `end`/`duration`**；重叠时下标最大者胜出，`layer_index` 被忽略。
- `compose`：`playing_clips(t)` 过滤 `clip.is_playing(t)`（`CompositeVideoClip.py:211-215`），标量形式 `(t >= start) and (t < end)`（`moviepy/Clip.py:598`）。参与集合由 `with_start(t)` 产生的 `end = start + duration` 决定（`Clip.py:246-250`）。

两条硬性差异：

1. **间隙**（`p > 0`）：`chain` 仍选中第 i 段，并把超出其时长的局部时间传进去；`compose` 无 clip 在播，输出背景。
2. **不看终点**：某段起点 ≤ t 但已结束，`chain` 仍可能选中它（若下标最大），`compose` 不会。这是钳位后出现分歧的机制。

### 3.3 边界

**【源码/实验】** `t = s_i`：两者都选 i。`t = D`：`chain` 的 `max` 取到哨兵下标 `n`，而 `clips` 只有 n 项 → `IndexError`；`compose` 返回背景。**【实验】** 两段 1 s、`p=0`：`get_frame(2.0)` 在 `chain` 抛 `IndexError`，`compose` 返回 `(0,0,0)`。

渲染时 `iter_frames` 只到 `int(D*fps)` 个点，最大 `t = (N−1)/fps < D`（`Clip.py:559-563`），所以写文件不触发该崩溃；**按时间直接取帧会**。

---

## 4. 画幅、位置、层序、帧率

### 4.1 `chain`：原样返回

**【源码】** `chain` 直接返回 `clips[i].get_frame(...)`（`:366`），没有 `compose_on`、没有画布、不用 `pos`。重叠区显示"下标最大的 clip 的原始帧"，尺寸是该 clip 的尺寸；`pos`、`layer_index` 不参与。

结果对象的 `size` 来自构造时的 `get_frame(0)`：`VideoClip.__init__` 在给了 `frame_function` 时执行 `self.size = self.get_frame(0).shape[:2][::-1]`（`moviepy/video/VideoClip.py:114-116`）。**`chain` 的声明画幅是"t=0 时被选中那一段"的画幅**——通常是第一段，但钳位后可能是最后一段。

**【实验】** 10×6 + 4×4、`p=−1`：`s=[0,0,1]`，`t=0` 已选中第二段，`chain.size=(4,4)`、`get_frame(0)` 是 4×4；`compose.size=(10,6)`。两者 `D` 都是 1。

### 4.2 `compose`：最大画布 + 居中 + 层序

**【源码】** `w/h` 取各段最大（`:354-357`）；每段 `with_start(t).with_position("center")`（`:382`）——**覆盖用户自设的 `pos`**；`CompositeVideoClip` 按 `layer_index` 稳定排序（`:116`），同层时列表靠后者后画（`:202-205` 顺序遍历 + `compose_on`）；小画幅留背景。

`compute_position` 末尾 `return (int(pos[0]), int(pos[1]))`（`moviepy/tools.py:313-314`），居中坐标是**截断**，奇偶不匹配会有半个像素的系统偏移。

**【实验】** 4×4 红底 + 2×2 绿块、用户设 `pos=(2,2)`：`chain` 在 `t=1.5` 返回**原始 2×2 绿帧**；`compose` 返回 4×4，绿块被居中到 (1,1)，四周为背景。

### 4.3 层序

**【源码】** `chain` 胜者 = `max(i: s_i ≤ t)`；`compose` 胜者 = `layer_index` 最大者，同层取列表后者。

**【实验】** 红（层 5）+ 绿（层 0）重叠：`chain` 输绿，`compose` 输红。同样的 `timings`、同样的 `D`。

### 4.4 帧率

**【源码】** 同 §3.1，取帧时间一致，`fps` 只影响输出网格。**【实验】** 10 fps 红 + 30 fps 绿、`p=−0.5`：两者在 `t=0.3/0.6/0.9/1.2` 的像素一致，`result.fps` 都是 30。**帧率不是分歧源。**

---

## 5. 透明遮罩：`chain` 的 padding 断链

### 5.1 遮罩管线与视频管线不同源

**【源码】** `chain` 发现任一 clip 有 `mask` 时：

```python
masks = [get_mask(clip) for clip in clips]                                  # :376
result.mask = concatenate_videoclips(masks, method="chain", is_mask=True)   # :377
```

**没有传 `padding`**，遮罩用 `padding=0` 重建。视频用 `s_i = max(0, C_i + i·p)`，遮罩用 `C_i`。`get_mask` 给无遮罩的 clip 补全不透明的 `ColorClip(color=1, is_mask=True)`（`:369`），并在 `duration` 为空时补 `clip.duration`（`:370-371`）。

`compose` 的遮罩在 `CompositeVideoClip.__init__` 按各 clip 的 `start/end` 重建（`:131-150`），与视频同源。`bg_color=None`（默认）时生成透明合成与 `mask`（`:82`、`:87-89`）；给了具体 `bg_color` 则不生成遮罩，把 alpha 直接混进背景色。

### 5.2 后果（两段各 2 s，`p=−1`）

**【实验】**

| 量 | `chain` | `compose` |
|---|---|---|
| 结果 `duration` | 3 | 3 |
| `mask.duration` | **4** | 3 |
| `t=1.5` 画面 | 绿 | 绿 |
| `t=1.5` 遮罩 | **0.2**（来自 A 的错位遮罩） | 1.0 |
| 写 webm 后逐帧 alpha（5 fps） | 51×10, 255×5 | 51×5, 255×10 |

写 mp4 时 alpha 被丢弃（ffmpeg 报 `Incompatible pixel format 'yuva420p' ... auto-selecting 'yuv420p'`），分歧在 mp4 上"看不见"；写 webm（`libvpx` + `yuva420p`，`ffmpeg_writer.py:153-156`）会原样暴露。**【实验】** 解码确认 51 vs 255；`ffprobe` 显示 `alpha_mode=1`。

**正 padding 也一样坏，而且方向相反。** 两段各 1 s、`A` 遮罩 0.5、`B` 遮罩 1.0、`p=+1`：**【实验】**

| 量 | `chain` | `compose` |
|---|---|---|
| 结果 `duration` | 3 | 3 |
| `mask.duration` | **2**（视频 3 s） | 3 |
| `t=1.5`（间隙，本应透明）alpha | **1.0** | 0.0 |

`chain` 的遮罩按 `C_i` 只排到 2 s，`compose` 的间隙是透明背景。也就是说 `chain` 把"本应透明的空隙"标成完全不透明，同时遮罩本身比视频短 1 s。**`padding=0` 是唯一让 `chain` 遮罩正确的取值。**

### 5.3 `padding=0` 时遮罩可替换；显式 `bg_color` 会破坏

**【源码/实验】** `p=0` 时遮罩 timings 与视频相同，两种方法的遮罩值一致。半透明绿（0.5）+ `p=0`：两者 `t=1.5` 的 RGB 都是 `(0,255,0)`、遮罩都是 0.5，写出 mp4 的 md5 相同。

若显式给 `bg_color`，`compose` 把 0.5 压进背景：`t=1.5` 得 `(0,127,0)`（`bg_color=(0,0,0)`）而非 `chain` 的 `(0,255,0)`，且 `compose.mask is None`。**【实验】** 仓库没有覆盖这一组合。

---

## 6. 音轨：方法无关，但与画面在重叠区不同步

### 6.1 构造完全共享

**【源码】** 音频在两分支合流后统一构造（`:400-406`）：对每段取 `clip.audio`，用**与视频相同的** `timings` 调 `with_start(t)`，再用 `CompositeAudioClip(...)` 包 `with_duration(result.duration)`。因此同一组 clips + 同一 padding，**音频对象逐样本与 method 无关**，起点与视频分段起点相同。

**【实验】** 不同频率立体声、`p=0/−1/+1`：两种方法在 1 ms 网格上 `np.array_equal` 为真；编码到 mp4 后解码 PCM，字节数与样本完全相同（立体声 44.1 kHz 路径）。

### 6.2 起点相同 ≠ 与画面对齐

**【源码】** 每段音频 `end = start + audio.duration`（`Clip.py:246-250`）；`CompositeAudioClip.frame_function` 对参与片段**相加**（`moviepy/audio/AudioClip.py:405-420`），不因视频是否在播而裁剪。结果被 `with_duration(result.duration)` 截断总长，但子片段本身未被钳到其视频段时长。

**【实验】** 两段各 2 s、`p=−1`、音 0.3/0.4：`t=1.5` 两种方法音频都是 `0.7`（叠加），画面都只有后一段。**切换 method 不改声音，但改变"这段声音对应哪一帧/哪几帧"。**

### 6.3 边界包含性：数组查询与标量查询不同

**【源码】** `Clip.is_playing` 标量用 `t < end`（`Clip.py:598`），数组用 `t <= end`（`Clip.py:592-595`）。`iter_chunks` 用数组时间轴（`AudioClip.py:108-113`），所以写文件时**恰在边界的样本会被两段同时计入**；`get_frame(标量)` 会排除。验证"声音是否相同"必须固定查询形式。

### 6.4 两个与 method 无关、但会污染输出证据的源码问题

- **单声道广播错误**：`clip.get_frame(...) * np.array([part]).T` 后 `zero + sum(sounds)`，当 `nchannels == 1` 且 `t` 是数组时，`sound` 形状 `(len,)`、`zero` 形状 `(len,1)`，广播成 `(len,len)`（`AudioClip.py:409-420`）。`to_soundarray` 因此返回 N×N 方阵。**【实验】** 声明 0.2 s 的拼接音频写成 wav 后为 320 s；声明 1 s 的写成 3555.56 s；混进 mp4 后容器音频时长 1459 s / 2918 s。立体声（`nchannels ≥ 2`）不触发。
- **编码失败静默返回**：8 kHz mp3 混流报 `muxing mp3 at 8000hz is not standard`、`Could not write header`，`write_videofile` 对 0 字节产物**不抛异常**。**【实验】** 产物 0 字节、函数正常返回。

---

## 7. 等价条件

记第 i 段视频 `F_i(τ)`、遮罩 `M_i(τ)`、时长 `d_i`，`s_i = max(0, C_i + i·p)`，`D = max(0, Σd + n·p) − p`。

### 7.1 判据 A：无重叠（最常用）

条件：`p = 0`；所有 `clip.size` 相同且帧形状 `(h,w,3)`；`bg_color` 默认 `None`。

则 `t ∈ [s_i, s_{i+1})` 时 `chain` 选 i，`compose` 的参与集合恰为 `{i}`（`s_i ≤ t < s_i+d_i`），在透明背景下 `compose_on` 退化为"取 `F_i`、取 `M_i`"（`VideoClip.py:839-857`：`bg` 的 mask 为 0 时 `result = frame*alpha + bg*(1-alpha)`，`bg=0` 即 `frame*alpha`；`bg_mask` 同步为 `alpha`）；音频逐样本相同（§6.1）。

**【实验】** 同尺寸、不透明、两段各 1 s、`p=0`：写出 mp4 的 **md5 完全相同**；半透明（0.5）+ `p=0` 也相同（含遮罩 0.5）。

**但"像素相同"不等于"输出相同"**：`chain` 在所有 clip 都无遮罩时 `result.mask is None`（`:375`），`compose` 在 `bg_color=None` 时**总是**生成遮罩对象（`:131-150`）。于是即使所有段全不透明，写 webm 时 writer 分别以 `rgb24` / `rgba` 为输入、请求 `yuv420p` / `yuva420p`（`ffmpeg_writer.py:110`、`:153-156`）。
**【实验】** 同两段、`p=0`：mp4 的 md5 相同，**webm 的 md5 不同**（`chain` 为 `yuv420p`，`compose` 为 `yuva420p`）；解码后的 RGBA 逐帧相同、alpha 全为 255。即"视觉等价、码流不等价"，是否算等价取决于下游是否读 alpha 通道。

例外：显式非 `None` 的 `bg_color` 会把 alpha 压平（§5.3），此时 `compose` 也不再生成遮罩，webm 的像素格式差异随之消失，但半透明像素会不同。

### 7.2 判据 B：允许重叠，但"顶层段必须覆盖且仍在播"

对每个 `t < D`，要求：

1. `chain` 选中的下标 i 满足 `s_i ≤ t < s_i + d_i`（该段仍在播）；
2. i 是所有在播段中 `layer_index` 最大者（同层时列表最靠后者）；
3. 该段不透明：`clip.size == (w,h)`、位置被居中到 `(0,0)`、且 `M_i ≡ 1`（无遮罩或遮罩恒 1）。

条件 3 使 `compose_on` 输出与 `chain` 的裸帧逐像素相同；条件 1 排除"段已结束但 `chain` 仍选中"；条件 2 使层序与"最大下标"一致。最常见的充分情形是：**所有段同尺寸、全不透明、且 `D` 等于最后一段的结束时刻**（即哨兵未被钳位）。

**【实验】** 三段各 1 s、全不透明：`p=0 / −0.5 / −1.0` 逐帧相等（`p=−1` 时 `s=[0,0,0]`、`D=1`，虽被钳位但最后一段恰在 `D` 结束，仍相等）；`p=−1.5` 时 `D=1.5` 而所有段在 1 s 结束，`t ≥ 1` 处 `chain` 仍画蓝、`compose` 回到背景 → 不等。

注意条件 1、2 的必要性来自两个方向：`chain` 的选择器只增不减（下标一旦前进就不会回退），`compose` 的参与集合则随 `end` 收缩。**任何"顶层段先结束、下层段仍在播"的时刻，两者必然分歧**——这正是 §8 第 7、9 行的形态。

### 7.3 判据 C：遮罩必须同 padding

任一 clip 有遮罩且 `p ≠ 0` 时，`chain` 的遮罩来自 `:377` 的零 padding 递归，与视频 timings 不同源，**不可替换**（§5）。`compose` 的遮罩与视频同源。

### 7.4 区间归属兼容表

| 区间 | `chain` 归属 | `compose` 归属 | 兼容 |
|---|---|---|---|
| `p=0` 的 `[s_i, s_i+d_i)` | 唯一段 i | 唯一段 i | **兼容** |
| `t = s_{i+1}` | i+1 | i+1 | **兼容** |
| `p<0` 且判据 B 成立 | 后段（最大下标） | 顶层段 | **兼容** |
| `p<0` 且后段半透明/不同尺寸/低层 | 后段裸帧 | 叠加结果 | 不兼容 |
| `p>0` 间隙 | 前段（局部时间越界） | 背景 | 不兼容 |
| 段已结束但仍被 `chain` 选中 | 该段 | 已不参与 | 不兼容 |
| `t = D` | `IndexError` | 背景 | 不兼容 |

**【实验】** `p>0` 间隙：`chain` 把前段延续到间隙（`ColorClip` 是常量；`VideoFileClip` 重复最后一帧，`ffmpeg_reader.py:191-221`；`BitmapClip` 直接 `IndexError`）。`compose` 输出背景 `(0,0,0)`；默认 `bg_color=None` 时 alpha 为 0。写 mp4 后解码确认 5 个间隙帧为黑。

---

## 8. 失败边界：总时长相同、画面/声音不同

先分清两种"同 D 不同内容"：

- **(M) 同一 padding、不同 method** —— 只有画面会不同（音频与 method 无关）。
- **(P) 不同 padding、相同 D** —— 画面和声音都可能不同；此类别里 method 之间通常一致。

以下各段除注明外时长 1 s。

| # | 输入 | 配置（D） | 现象 | 类别 |
|---|---|---|---|---|
| 1 | 两段 1 s | `p=0` vs `p=−2`（D 都是 2） | `t=0.5`：前者红，后者绿；两种 method 各自一致 | P |
| 2 | 两段 1 s | `p=+1`（D=3） | `t=1.5`：`chain` 红（延续前段），`compose` 黑（间隙） | M |
| 3 | 两段 1 s | `p=−3`（D=3） | `t=1.5`：`chain` 绿（持续选中后段），`compose` 黑（两段都已在 1 s 结束） | M |
| 4 | 两段 1 s，后者 0.5 透明 | `p=−0.5` | 重叠区 `chain` 纯绿、alpha 1.0；`compose` RGB `(127,127,0)`、alpha 仍 1.0（0.5 叠在不透明红上） | M |
| 5 | 10×6 + 4×4 | `p=−1`（D=1） | `chain` 返回 4×4 裸帧；`compose` 10×6 且绿块居中 | M |
| 6 | 红层 5 + 绿层 0 | `p=−0.5` | `chain` 绿；`compose` 红 | M |
| 7 | 10 s + 1 s + 0.5 s | `p=−5`（D=5） | `chain` `[0,1)` 红、`[1,5)` 蓝（末段被反复选中）；`compose` `[0,1)` 红、`[1,1.5)` 蓝、`[1.5,5)` **回到红**（首段仍未结束且在底层） | M |
| 8 | 两段各 2 s，各有遮罩 | `p=−1`（D=3） | `chain` 遮罩 4 s、`t=1.5` 值 0.2；`compose` 遮罩 3 s、值 1.0 | M |
| 9 | 10 s + 1 s + 10 s | `p=−8`（D=8） | `t>8`：`chain` `IndexError`；`compose` 仍出蓝帧（末段 10 s 未结束） | M |
| 10 | 两段 1 s，音 0.3 / 0.4 | `p=−3` vs `p=+1`（D 都是 3） | `t=0.3`：前者音频 0.7、画面绿；后者音频 0.3、画面红 | P |
| 11 | 单声道任意两段 | 任意 | 写出的音频样本数被放大（N×N） | 与方法、padding 均无关 |

补充：**method 之间永远不产生音频差异**（§6.1）——同一 padding 下两者 PCM 相同（已用立体声逐样本验证）。因此"总时长一致却声音不同"只能来自 padding/输入差异（#10），不能来自切 method。

---

## 9. 三层责任划分

| 层 | 发生位置 | 受 method 影响 |
|---|---|---|
| 时间安排 | `timings`（`:352-360`）、结果 `duration/start/end`（`:398`）、音频起点（`:400-406`） | **否** |
| 逐帧选择与合成 | `chain` `:364-366`；`compose` `:380-388` + `CompositeVideoClip.frame_function` `:156-215` + `VideoClip.compose_on` `:720-902` | **是**（选择器、层序、透明度、画布、遮罩来源） |
| 编码输出 | `Clip.iter_frames`（`Clip.py:516-571`）、`ffmpeg_writer.py:85-320`、`ffmpeg_audiowriter.py` | **间接**（形状/遮罩/尺寸分歧在此显形） |

编码层机制：

1. **帧数只由 `duration` 和 fps 决定**：`np.arange(0, int(self.duration * fps))`（`Clip.py:559`）。`duration` 相同 → 帧数相同。`fps` 默认取 `clip.fps`，两分支都设为 max（`:408-409`），不会因缺省而不同。
2. **尺寸取自 `clip.size`，不校验每帧形状**：writer 以 `-s WxH` 告知 ffmpeg（`ffmpeg_writer.py:122-124`），`write_frame` 只写字节、不校验长度（`:181-191`）。`chain` 的 `size` 来自 `get_frame(0)`（`VideoClip.py:114-116`），一旦与后续帧形状不符，ffmpeg 收到错误 packet。
   **【实验】** 10×6 红 + 4×4 绿、`p=0`（同 D=2）：`compose` 写出 2.0 s / 10 帧；`chain` 得到 1.2 s / 6 帧，日志 `Invalid buffer size, packet size 60 < expected frame_size 180`，但 `write_videofile` **正常返回**。反转顺序（先 4×4 后 10×6）：`chain` 产物 4.6 s / 23 帧。
3. **遮罩决定像素格式与 alpha**：writer 以 `clip.mask is not None` 选 `rgba` 输入（`ffmpeg_writer.py:110`、`:289-315`），逐帧 `np.dstack([frame, 255*mask])`。`libx264` 分支请求 `yuva420p`（`:157-162`）会被 ffmpeg 降级为 `yuv420p`；`libvpx + with_mask` 才保留 `yuva420p`（`:153-156`）。**"有没有 mask 对象"在此比"mask 值是多少"更关键**：`chain` 仅在有 clip 带遮罩时才建遮罩（`:375`），`compose` 在 `bg_color=None` 时无条件建（`:131-150`）。于是即便内容逐像素相同，mp4 的 md5 可相同、webm 的却不同（§7.1）。判断"是否等价"必须先固定容器/编码，甚至先固定"是否把 alpha 计入"。**【实验】** 同两段 `p=0`：mp4 md5 相同，webm md5 不同，解码 RGBA 相同、alpha 全 255。
4. **音频独立编码再混流**：`write_videofile` 先 `self.audio.write_audiofile(...)`，再以 `audio_codec="copy"` 交给 ffmpeg（`VideoClip.py:378-391`、`ffmpeg_writer.py:132-135`）。音频分歧只在"写入的样本"层，不在混流层。
5. **量化/色度下采样** 使"逐像素等价"不能靠 md5 推广到任意编码器；本文的 md5 相同结论只在 `libx264 + ultrafast + 纯色小画幅` 下成立。

---

## 10. 证据分级

### 10.1 源码事实

- `:352-360`：timings 公式；`padding` 对两分支共用；无自动切换。
- `:364-366`：`chain` 只看起点，重叠取最大下标，局部时间不做终点钳位。
- `:374`：`chain` 结果是 `VideoClip`；`:377`：遮罩递归不使用 `padding`；`:375`：仅当某段带遮罩时才建遮罩。
- `:380-388`：`compose` 强制居中、画布取最大宽高。
- `:398-409`：`duration/audio/fps` 收尾，与 `method` 无关。
- `CompositeVideoClip.py:116`（层序排序）、`:131-150`（遮罩按各段 `start/end` 重建；`bg_color=None` 时无条件建）、`:211-215`（`is_playing` 过滤）。
- `Clip.py:598`（标量 `< end`）与 `:592-595`（数组 `<= end`）。
- `VideoClip.py:114-116`（`size` 来自 `get_frame(0)`）、`:755-758`（`compose_on` 局部时间）。
- `ffmpeg_writer.py:110/122-124/153-162/181-191/289-315`（尺寸、像素格式、alpha、不校验帧长）。
- `ffmpeg_reader.py:191-221/271-277`（EOF 复用最后一帧、按自身 fps 量化）。
- `AudioClip.py:405-420`（求和与单声道广播）。
- 文档与代码不符：`CompositeVideoClip.py:339-344`、`docs/user_guide/compositing.rst:35/58`。

### 10.2 现有测试的支撑范围

| 测试 | 实际断言 | 对本文的价值 |
|---|---|---|
| `tests/test_compositing.py:66` `test_concatenate_self` | 单段 `BitmapClip` 与目标相等 | 仅证明 `p=0`、单段可用 |
| `tests/test_compositing.py:78` `test_concatenate_floating_point` | 只调用 `write_videofile` | 1.12 s 浮点边界能写出；**不查帧数/内容** |
| `tests/test_compositing.py:300` `test_concatenate_with_masks` | 只调用 `write_videofile` | 混尺寸 + 遮罩"跑通"；**【实验】** 该输出 ffmpeg 报 `Invalid buffer size`，产物只有 1 s / 1 帧 |
| `tests/test_issues.py:20` `test_issue_145` | 非法 method 抛异常 | 仅参数校验 |
| `tests/test_issues.py:40` `test_issue_285` | `duration == 30` | 仅时间安排 |
| `tests/test_issues.py:315` `test_issue_547` | `duration` 与 `mask.duration` | `p=0` 的经时；**无重叠/间隙** |
| `tests/test_issues.py:259/286` | 拼接后 `fps` 为 max / None | 仅 fps 汇总 |
| `tests/test_ffmpeg_writer.py:187` `test_write_gif` | 三段 0.1 s 拼后取色 `t=0/0.1/0.2` | **唯一**带内容断言，但仍是 `p=0`、同尺寸、不透明 |
| `tests/test_AudioClips.py:54` | 拼接音频能写文件、时长相加 | 未读回文件；**【实验】** 输出 wav 实际 4.41 s（声明 0.01 s） |
| `tests/test_SubtitlesClip.py:28`、`tests/test_videotools.py:307` | 用拼接构造输入 | 不比较两种 method |

**【实验】** 本地执行：`test_compositing.py` 10 passed；`test_issues.py` 19 passed；`test_videotools.py` 55 passed / 1 skipped；`test_VideoClip.py + test_ffmpeg_writer.py + test_SubtitlesClip.py` 151 passed；`test_AudioClips.py` 16 passed。**全绿不覆盖本文列出的任何一条分歧。**

`grep -rn "padding" tests` 只命中 `test_fx.py` 的 `padding_end`（`Freeze` 参数）与文本渲染的 padding：**没有任何测试对 `concatenate_videoclips` 传 `padding`**，也没有任何测试同时比较 `chain` 与 `compose`。

### 10.3 仍缺的媒体输出证据（【缺口】）

1. **解码帧对拍**：没有把两种 method 各写一份、再解码逐帧比较的测试或黄金文件。本文对照只是本地一次性实验，未入库。
2. **PCM 对拍**：没有对混流后的音轨做逐样本比较；现有音频测试只断言"文件存在"和"对象时长相加"。
3. **alpha 通道验证**：没有测试解码 webm 的 `yuva420p`；`test_concatenate_with_masks` 写 mp4，恰好把 alpha 丢掉，对该行为几乎没有约束力。
4. **间隙/钳位/非单调**：`padding>0`、`|p|` 超阈值、`timings` 非单调三类输入没有任何测试。
5. **写文件帧数守恒**：没有断言 `nb_frames == int(duration*fps)`；混画幅的 `chain` 正是在这里静默丢帧。
6. **多容器一致性**：mp4/webm/mkv 之间没有交叉验证；本文只测 mp4 与 webm。已观察到 `p=0`、全不透明时 mp4 的 md5 相同而 webm 不同，这一"同一内容、不同码流"的现象没有测试固定。
7. **"是否有 mask 对象"的语义**：`chain` 仅在有 clip 带遮罩时建遮罩，`compose` 默认总建；没有测试断言这一点，也没有测试据此判断输出像素格式。
7. **真实文件素材的越界行为**：EOF 复用最后一帧只在 `VideoFileClip` 路径生效，没有测试把"正 padding + `VideoFileClip`"的输出与"compose 背景"对比。
8. **版本/平台矩阵**：ffmpeg 版本、编码器、`sws_flags` 都会影响量化结果；仓库没有回归基线。

---

## 11. 建议补齐的测试（按投入产出排序）

1. **判据 A 回归**：同尺寸、不透明、`p=0`、多段，断言两种 method 的 `iter_frames` 逐帧相等，并断言写出文件解码帧相等。断言以"解码后的 RGBA/RGB 数组"为准，**不要**以文件 md5 为准——webm 的像素格式会不同。
2. **判据 B 回归与反例**：不透明重叠（应相等）vs 半透明重叠（应不等，并给出期望像素）。
3. **遮罩对齐**：任意 `p≠0` + 遮罩时断言 `result.mask.duration == result.duration`，并断言边界处遮罩值。当前 `:377` 会直接失败，这是最值得先修的缺陷。
4. **间隙与钳位**：`p>0` 的间隙像素、`|p| > Σd/n` 的 `D` 公式、非单调 `timings` 下的选择器行为。
5. **混画幅写文件**：断言 `chain` 要么显式报错、要么输出帧数与 `int(D*fps)` 相符；目前是静默截断。
6. **音频**：写文件后读回 PCM，断言样本数 = `int(fps*duration)`（会暴露单声道广播问题），并断言两种 method 的 PCM 相同。
7. **边界查询**：明确 `get_frame(D)` 的契约（`chain` 当前抛 `IndexError`），并在测试中固定标量/数组两种 `is_playing` 的边界语义。

---

## 附录 A. 最小复现

以下片段是本次分析的思路（未入库；`moviepy` 需从仓库根目录导入）。

```python
import numpy as np
from moviepy import *
from moviepy.video.compositing.CompositeVideoClip import concatenate_videoclips

A = ColorClip((4, 4), (255, 0, 0)).with_duration(1).with_fps(5)
B = ColorClip((4, 4), (0, 255, 0)).with_duration(1).with_fps(5)

# 同 D、不同内容（跨 padding）
for p in (0, -2):
    c = concatenate_videoclips([A, B], padding=p)
    print(p, c.timings, c.duration, c.get_frame(0.5)[0, 0])

# 同 D、不同内容（跨 method）：p=+1 的间隙
for m in ("chain", "compose"):
    c = concatenate_videoclips([A, B], padding=1, method=m)
    print(m, c.duration, c.get_frame(1.5)[0, 0])
```

- 遮罩错位：给每段挂 `ColorClip(..., is_mask=True)`，`p=-1`，比较 `c.mask.duration` 与 `c.duration`。
- 编码：`write_videofile(..., fps=5, preset="ultrafast")` 后用 `ffprobe` 看 `nb_frames`，并检查 `*.log` 里的 `Invalid buffer size`。
- 码流 vs 像素：同两段全不透明、`p=0`，分别写 mp4 与 webm；mp4 的 md5 相同、webm 的 md5 不同，而解码后的 RGBA 相同。比较等价性时应解码后比数组，而不是比文件哈希。

这些实验依赖本机 ffmpeg 9.0.2 与 `libx264`/`libvpx`，只能作为"存在分歧"的存在性证据，不能当跨版本、跨容器的黄金结果。仓库当前没有可继承的媒体基线，§10.3 的缺口仍未闭合。
