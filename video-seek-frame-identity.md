# 非整数帧率下的视频定位与帧同一性分析

分析对象：仓库当前代码（分支 `task/58`），核心文件
`moviepy/video/io/ffmpeg_reader.py` 中的 `FFMPEG_VideoReader`，配合
`moviepy/Clip.py`、`moviepy/video/VideoClip.py`、
`moviepy/video/io/VideoFileClip.py` 以及 `tests/test_ffmpeg_reader.py`。

本文只讨论「同一文件、同一 `FFMPEG_VideoReader` 实例（或对同一文件以相同
参数新建的实例）」下的取帧行为，不涉及滤镜链上的帧变换。

---

## 0. 结论速览

1. **逻辑帧位置只由 `t` 和 `fps` 决定，与访问历史无关。**
   `get_frame_number(t) = int(fps*t + 1e-5)`（ffmpeg_reader.py:271-277）是纯
   函数。因此同一请求时间 `t` 在任何访问历史下算出的逻辑帧号 `n` 必然相同。
2. **真实返回的素材帧是否相同，取决于命中的三条路径中的哪一条**
   （缓存命中 / 顺序跳过 / ffmpeg 重新定位，见第 3 节）。源码能保证逻辑帧号
   一致；「不同路径解码出同一物理帧」这一点，源码只能在整数帧率测试素材上
   给出经验证据（`test_large_small_skip_equal`），非整数帧率下属于需要实际
   解码证据的部分（第 6 节）。
3. **两类误差必须分开：**
   - *时间换算误差*：浮点表示与 `1e-5` 修正、6 位小数格式化，发生在 Python
     侧，确定且可静态推演；
   - *解码定位边界*：ffmpeg `-ss` 实际落到哪个关键帧/PTS，发生在 ffmpeg
     侧，源码无法确定，只能靠 `-1e-5` 回退提高命中率。
4. **读取失败后的返回是「上一次成功解码的 `last_read`」，不是新数据。**
   首次读取（无 `last_read`）失败是硬错误 `IOError`；此后任何短读都只发
   `UserWarning` 并重复末帧，且 `pos` 照常自增（ffmpeg_reader.py:191-234）。
5. **重复末帧是既有、被测试固定下来的回退行为**
   （`test_seeking_beyond_file_end`），不是新缺陷；但回退帧可能并非「紧邻
   失败点的前一帧」，因为 `skip_frames` 不更新 `last_read`（见 5.3）。

---

## 1. 关键数据结构与不变量

`FFMPEG_VideoReader`（ffmpeg_reader.py:20-292）维护以下与定位相关的状态：

| 属性 | 含义 |
| --- | --- |
| `self.fps` | 从 ffmpeg 输出解析并归一化后的帧率（可能是非整数，如 `30000/1001`） |
| `self.pos` | **下一个**将被 `read_frame()` 读取的帧的 0 基逻辑序号；即「读完当前帧之后的位置」 |
| `self.last_read` | 最近一次**成功** `read_frame()` 得到的 numpy 数组（初始化时预读第一帧） |
| `self.n_frames` | `int(duration * fps)`，元数据估算值，并非实测可解码帧数（ffmpeg_reader.py:730-732） |
| `self.proc` | 当前 ffmpeg 解码子进程；`None` 表示未打开 |

两个文档化注释确立了语义基线：

- ffmpeg_reader.py:94-97：`self.pos` 是「下一个待读帧」的索引，初始
  `initialize(0)` 预读第一帧后 `pos == 1`（由 `test_sequential_frame_pos`
  断言，tests:611）。
- ffmpeg_reader.py:99-109：moviepy 的 seek 语义是「取在时刻 `t` 正在显示的
  帧」，而 ffmpeg 的 `-ss` 语义是「跳过直到到达 `t`」。两者在整数帧率、时间
  戳可精确表示时一致；在 29.97 这类帧率下会差一帧，因此给 ffmpeg 的时间戳要
  减去一个 epsilon。

`VideoFileClip` 只是把调用转发给 reader：
`self.frame_function = lambda t: self.reader.get_frame(t)`
（VideoFileClip.py:144）。`Clip.get_frame` 的 `memoize` 默认 `False`
（Clip.py:58、80-89），库内没有任何地方把它打开，因此默认不存在第二层缓存，
每次调用都进入 reader。

---

## 2. 请求时间 → 逻辑帧位置：换算层（源码可确定）

### 2.1 帧率本身先被「修正」为非整数有理数

`FFmpegInfosParser._parse_stream_video`（ffmpeg_reader.py:606-628）：

- 优先取 `fps` 字段，失败再取 `tbr`；
- 对 `x ∈ {23,24,25,30,50}`，若解析值与 `x*1000/1001` 的差小于 `0.01`，
  则强制改写为 `x*1000/1001`。ffmpeg 常把 `30000/1001` 打印成 `29.97`，
  这一规则把它还原为精确的 `29.9700299700…`。

数值验证（按源码规则复算）：`29.97` 与 `30000/1001` 相差约 `3.0e-05 < 0.01`，
会被归一化；而 `29.91` 不在带内，保持原值。也就是说，进入定位计算的
`self.fps` 通常是一个**二进制浮点无法精确表示的循环小数**，这是后续所有浮点
现象的根源。

### 2.2 逻辑帧号公式与两个 `1e-5`

```python
def get_frame_number(self, t):
    return int(self.fps * t + 0.00001)          # ffmpeg_reader.py:271-277
```

- `int(...)` 向零取整，等价于对非负 `t` 向下取整。理想语义是
  `n = floor(t / (1/fps))`，即帧 `n` 覆盖时间带
  `[n/fps, (n+1)/fps)`（下端含、上端不含）。
- `+1e-5` 是注释明确说明的浮点保护：`n/fps` 在数学上应恰为边界，但浮点计算
  可能得到 `2.99999999…` 而被错截到前一帧。

对 29.97 fps 的复算结果：

- 对 `n = 1, 2, 100, 1000`，`get_frame_number(n/fps)` 全部正确回到 `n`；
- 保护窗口很窄：在理想边界下方仅 `1µs` 处（`n/fps - 1e-6`）就已经落到
  `n-1`。即 epsilon 只能吸收 **10 µs 量级以下**的浮点下溢，不能把任何真实的
  时间提前量「拉回」当前帧。这是换算层的确定性边界，不是可调阈值。

因此，多个不同的请求时间映射到同一逻辑帧号是**设计使然**：整个时间带
（宽度约 `1/fps ≈ 0.03337 s @29.97fps`）内的 `t` 都返回同一个 `n`。
`iter_frames` 用 `t = frame_index / fps`（Clip.py:558-565）逐帧取数，配合该
公式得到 0,1,2,… 单调不重复的帧号序列（已用 `np.arange` 网格复算验证：
30 个时间点对应 30 个唯一帧号 0..29）。

### 2.3 换算误差清单（Python 侧，确定）

1. `fps` 本身是有理数的浮点近似（如 `30000/1001`）；
2. `fps*t` 的乘法与取整；`+1e-5` 只补偿下溢，不改变时间带语义；
3. `initialize` 把 seek 时间格式化为 6 位小数
   （`"%.06f"`，ffmpeg_reader.py:117-124），量化到微秒。对 29.97 fps，
   `n/fps - 1e-5` 与精确值的偏差稳定在约 `9.67 µs`（即 epsilon 主导），
   6 位小数舍入再引入亚微秒级误差。

这三项都可以在不运行 ffmpeg 的情况下静态/数值复现，属于**时间换算误差**。

### 2.4 帧数估算与逻辑域的边界

`n_frames = int(duration * fps)`（ffmpeg_reader.py:730-732），与
`get_frame_number(duration)` 在数学上同源。对 10 s、29.97 fps 估算为 299 帧
（合法逻辑号 0..298）。但这只是容器元数据乘积的截断值，**不保证管道实际能
吐出这么多完整帧**；真实可读帧数只能由解码决定（见第 5、6 节）。

---

## 3. 三条取帧路径：访问历史如何影响真实画面

`get_frame(t)`（ffmpeg_reader.py:236-264）先算
`pos = get_frame_number(t) + 1`（加 1 后表示「读完该帧之后的位置」，便于与
`self.pos` 直接比较），然后按当前 `self.pos` 分三条路径：

### 路径 A：缓存命中 —— `pos == self.pos`（254-255 行）

直接 `return self.last_read`，不触碰管道，不推进 `pos`，**不产生新解码**。
同一逻辑帧的第二次请求是零成本的同对象返回（numpy 数组本身，未拷贝）。
`test_unusual_order_frame_pos` 中连续两次 `get_frame(4)` 返回同一白帧
（tests:644-646）即固定此行为。

### 路径 B：顺序前进 —— `self.pos < pos <= self.pos + 100`（260-264 行）

```python
self.skip_frames(pos - self.pos - 1)   # 原地读字节并丢弃
result = self.read_frame()             # 真正解码/读出目标帧
```

- `skip_frames`（171-178 行）只 `read(nbytes)` 原始字节后丢弃并推进
  `pos`，**不更新 `last_read`**；
- 相邻帧时跳过数为 0，注释明确「this line has no effect」（261 行）。

### 路径 C：重新定位 —— `pos < self.pos`（后退）或 `pos > self.pos + 100`
（超过 100 帧的远距离跳转，256-259 行）

调用 `self.initialize(t)`：终止旧进程、以新的 `-ss` 参数重启 ffmpeg，预读
目标位置第一帧到 `last_read`，然后返回它。任何后退（哪怕只退一帧）都走这条
路径——源码模拟验证：当前 `pos=150` 时请求前一帧（逻辑 148 → `pos=149`）
即触发重新定位。

`initialize` 内部（99-126 行）把 moviepy 语义翻译成 ffmpeg seek：

```python
if self.pos != 0:
    start_time = self.pos * (1/fps) - 0.00001   # 第二个 1e-5，作用在 seek 侧
else:
    start_time = 0.0
```

随后拆成输入侧/输出侧双 seek：`offset = min(1, start_time)`，命令行为
`-ss <start_time-offset> -i <file> -ss <offset>`（115-126 行）。输入侧 seek
借关键帧快速跳转，输出侧 seek 做精确解码定位。

### 3.1 路径选择对「请求时间 → 真实画面」的影响

| 维度 | 路径 A 缓存 | 路径 B 顺序跳过 | 路径 C ffmpeg 重定位 |
| --- | --- | --- | --- |
| 触发条件 | 同逻辑帧重复 | 前进且跨度 ≤100 逻辑帧 | 后退，或前进 >100 逻辑帧 |
| 是否新解码 | 否 | 是（连续管道） | 是（新进程 + `-ss`） |
| `last_read` 变化 | 不变 | 目标帧成功后更新 | 预读帧覆盖旧值 |
| 定位依据 | 无 | 管道当前字节位置 | `n/fps - 1e-5` 的 6 位小数时间戳 |
| 源码可确定的部分 | 同一数组对象 | 读取严格后移 `pos-self.pos` 个帧位 | ffmpeg 收到的时间戳数值 |
| 源码不能确定的部分 | — | 丢弃字节是否恰好对应帧边界（依赖管道健康） | ffmpeg 实际输出的物理帧是哪一帧 |

---

## 4. 同一请求时间、不同访问历史：何时得到同一素材帧

把问题拆成两层。

### 4.1 逻辑帧同一性（源码可确定，必然成立）

`get_frame_number` 是纯函数：只要 `t` 与 `fps` 相同，无论之前走过 A/B/C 哪条
路径、无论 `self.pos` 在哪，算出的 `n` 与 `pos=n+1` 都相同。因此**逻辑帧
位置对访问历史完全不敏感**。`test_large_skip_frame_pos`（tests:659-672）以
24 fps 素材固定了这一点：前进 240 帧、再到 719、再后退到 120，每次断言
`reader.pos` 与时间换算出的位置一致。

### 4.2 物理素材帧同一性（分条件）

设两次请求的逻辑帧号同为 `n`，但访问历史不同。

**条件 1 —— 至少一次命中路径 A：** 返回的是 `last_read` 引用。只要两次之间
没有发生过改变 `last_read` 的成功 `read_frame`/`initialize`，两次拿到的是
同一个 numpy 对象，像素必然逐位相同（源码可保证）。

**条件 2 —— 两次都走路径 B（不同顺序起点）：** 同一连续管道按字节顺序后移，
帧边界由 ffmpeg 管道的恒定帧大小（`depth*w*h` 字节，rawvideo）保证。只要管道
未发生短读，第 `n` 次读出的字节块就是同一帧——这是 rawvideo 定长分包决定的，
源码层面可推断成立。

**条件 3 —— 一次路径 B、一次路径 C（或两次路径 C 经过不同 seek 点）：**
源码只能保证两条路径**意图指向同一逻辑帧 `n`**：

- 路径 B 依赖「从当前位置顺序数 `n` 个定长帧」；
- 路径 C 依赖 ffmpeg 对时间戳 `n/fps - 1e-5` 的双 `-ss` 定位，先跳到关键帧
  再解码到该时间点。

两者是否真正输出同一物理帧，取决于 ffmpeg 的 seek 落点与 PTS 对齐，**源码
无法判定**。仓库内的经验证据是 `test_large_small_skip_equal`（tests:675-701）：
对 24 fps、10 秒的 webm，分别用「逐帧顺序」「每秒小跳」「一次 240 帧大跳
（触发重定位）」三种历史读到 `t=10`，断言三幅画面 `np.array_equal` 全部
通过。但该证据只覆盖**整数帧率**；对 29.97 fps，仓库只有
`test_frame_seek`（tests:831-838，断言 `t=0` 与 `t=0.34` 两帧不同），
**没有任何跨历史同一性断言**。

**综合判据：**

> 同一请求时间在不同访问历史下得到同一素材帧，当且仅当（a）逻辑帧号相同
> （恒成立），且（b）实际解码路径在 ffmpeg 的 PTS/关键帧语义下收敛到同一
> 物理帧。(a) 由源码保证；(b) 在整数帧率素材上有测试背书，在非整数帧率、
> VFR（可变帧率）或 seek 点贴近关键帧边界时需要实测解码证据。

### 4.3 时间换算误差 vs 解码定位边界（明确分界）

| | 时间换算误差 | 解码定位边界 |
| --- | --- | --- |
| 发生位置 | Python：`get_frame_number`、`initialize` 的时间戳构造 | ffmpeg：双 `-ss`、关键帧跳转、PTS 对齐 |
| 典型量值 | 浮点下溢（<10 µs，被 `+1e-5` 吸收）、6 位小数量化（µs 级） | 可达一个完整帧间隔（~33.4 ms @29.97fps）甚至关键帧间隔 |
| 能否静态确定 | 能，可用纯 Python 复算 | 不能，依赖容器/编码器/ffmpeg 版本 |
| 代码中的应对 | 两个 `1e-5`（取整保护 + seek 回退） | 仅靠 seek 侧 `-1e-5` 把请求点移到帧 PTS 之前，把选择权交给 ffmpeg |
| 错误表现 | 相邻帧号错配（差一帧），可通过构造 `t=n/fps` 规避 | seek 落在错误关键帧后输出偏移帧，Python 侧无感知 |

`initialize` 里的 seek 回退（ffmpeg_reader.py:111）是两层之间唯一的桥：它把
换算层算出的精确边界主动后撤 10 µs，使 ffmpeg 的「到达 `t` 即停」语义近似
moviepy 的「`t` 时刻正在显示的帧」语义。注释（99-109 行）明确承认这是
「getting around a difference」，即启发式补偿而非严格等价。

---

## 5. 文件尾部、短读与失败后的缓存返回

### 5.1 短读检测与两条分支（ffmpeg_reader.py:189-234）

```python
s = self.proc.stdout.read(nbytes)
if len(s) != nbytes:                 # EOF 或截断：短读
    warnings.warn(... "Using the last valid frame instead.", UserWarning)
    if not hasattr(self, "last_read"):
        raise IOError(...)           # 首次读取失败：硬错误
    result = self.last_read          # 否则：重复上一有效帧
else:
    result = np.frombuffer(s, ...)
    self.last_read = result          # 只有成功才刷新缓存
self.pos += 1                        # 无论成败，pos 都自增
```

要点：

1. **失败不刷新 `last_read`、不抛异常（除首次外）**，调用方拿到的是旧数组；
2. **`pos` 无条件自增**，警告里的帧序号与时间（`self.pos/self.fps`，
   204 行）报告的是「失败的逻辑位」。对 29.97 fps、逻辑位 720 的失败，警告
   时间约为 `24.024 s`，而不是请求时间本身；
3. 警告每次短读都发一次（模拟状态机验证：连续 3 次短读产生 3 次警告、
   3 次同一对象返回，`pos` 持续增长）。Python 的 warning 默认去重，但显式
   `pytest.warns`/自定义过滤器下每次调用都可见。

### 5.2 首次读取失败：唯一的硬错误

`last_read` 在两个时机建立：构造函数末尾 `initialize()` 的预读
（ffmpeg_reader.py:169）。若该预读即短读，对象上还没有 `last_read` 属性，
抛出 `IOError`（209-219 行），文案指向文件损坏或 ffmpeg 版本过旧。
由于 `VideoFileClip.__init__` 同步构造 reader（VideoFileClip.py:113-120），
此错误会直接使**剪辑对象构造失败**，调用方拿不到可用实例——这是兼容性上最
硬的失败模式。

一个源码层面的不对称需要指出：后续任何一次 `initialize(t)` 重定位都以
`self.close(delete_lastread=False)`（92 行）开头，**刻意保留旧 `last_read`**。
因此重定位后的预读若短读，不会走 `IOError` 分支（属性仍在），而是发警告并
返回重定位之前的旧帧。「硬错误」只可能发生在整个 reader 生命周期的第一次
读取。

### 5.3 末帧附近的错误可见性与兼容判据

**可见性（源码可确定）：**

- 越过文件末尾的请求**不会越界报错**：短读被吞掉，代之以
  `UserWarning` + 旧帧，且逻辑位置继续前进。`test_seeking_beyond_file_end`
  （tests:704-721）固定了这一契约：
  - 5 帧、1 fps 的 `test_video.mp4`，`get_frame(5)` 产生
    「Using the last valid frame instead」警告，`pos == 6`；
  - 24 fps、30 s、720 帧的 webm，`get_frame(30)` 警告且
    `pos == 30*24+1 == 721`。
- 两个断言都比较 `end_of_file_frame == frame_1`（首帧），但两条用例命中的
  路径不同，恰好覆盖了两种回退来源（分支选择已按源码阈值复算）：
  - 5 帧、1 fps 的 `test_video.mp4`，`get_frame(5)`：`pos=6`、当前
    `self.pos=1`，跨度 5 ≤ 100，走**路径 B**——`skip_frames(4)` 丢弃 4 个
    帧字节（不更新 `last_read`），随后 `read_frame()` 短读。此时
    `last_read` 仍是初始化预读的首帧，故返回首帧，`pos == 6`；
  - 24 fps、30 s、720 帧的 webm，`get_frame(30)`：`pos=721`、当前
    `self.pos=1`，跨度 720 > 100，走**路径 C**——重定位后预读即 EOF，
    而 `initialize` 以 `delete_lastread=False` 保留了首帧，故同样返回首帧，
    `pos == 30*24+1 == 721`。

**兼容判据（判断「重复末帧」是否属于既有回退）：**

1. 现象可复现于当前仓库测试即属既有行为：警告文案
   「Using the last valid frame instead」与 `pos` 自增规则已被
   `pytest.warns` 断言固定，改动它会破坏 `test_seeking_beyond_file_end`；
2. 但「返回的旧帧」的身份有坑：若失败前走过路径 B 的 `skip_frames`，被跳过
   的帧从不更新 `last_read`，短读时返回的是**跳过批次之前**最后一个真正
   `read_frame` 成功的帧，而不是逻辑上紧邻失败点的前一帧。因此：
   - 想兼容「末帧冻结」语义的下游代码，应以「收到警告且返回值等于此前某个
     已读帧」为判据，**不能**假设返回帧的逻辑号等于 `n-1`；
   - 想严格检测越界，应自行比较请求逻辑号 `get_frame_number(t)` 与
     `n_frames`，或捕获该 `UserWarning`，不能依赖返回值区分；
3. `n_frames` 是元数据估算（2.4 节），与真实 EOF 可能不一致：估算偏多会
   提前进入回退，估算偏少则永远读不到某些逻辑号。哪一种发生属于解码证据
   范畴（第 6 节）。

### 5.4 缓存返回的身份语义

- 路径 A 与短读回退返回的都是 `last_read` **同一对象**，无拷贝；调用方若
  原地修改数组，会污染后续缓存返回。源码未做防御性拷贝；
- `close(delete_lastread=True)`（默认，279-289 行）会删除 `last_read`；
  但重定位内部用 `delete_lastread=False`，保证回退帧始终可用。
  `__del__` 走默认 close，属性随对象回收。

---

## 6. 源码能确定 vs 需要实际解码证据

### 6.1 仅凭源码即可确定的结论

1. `t → n = int(fps*t+1e-5)` 的全部映射性质：时间带划分、epsilon 保护窗口
   （<10 µs）、`n/fps` 的往返一致性、非负 `t` 的单调不重复网格；
2. 三条路径的选择条件（含 `+100` 帧阈值、任何后退都重定位）与各自对
   `pos`/`last_read`/管道的副作用；
3. 重定位时 ffmpeg 收到的确切时间戳数值（`n/fps-1e-5`，6 位小数）与双
   `-ss` 的拆分方式（`offset=min(1, ...)`）；
4. 短读的控制流：警告文案、首次 `IOError`、其后返回旧 `last_read`、
   `pos` 照常自增、警告按次发射；
5. `skip_frames` 不更新 `last_read` 这一缓存身份细节，以及重定位保留
   `last_read` 的不对称性；
6. `n_frames`/`duration` 来自容器元数据乘积截断，不是实测值；
7. 整数帧率测试素材上的经验事实：顺序/小跳/大跳三条历史在 `t=10` 输出
   相同画面（`test_large_small_skip_equal`），末帧回退契约
   （`test_seeking_beyond_file_end`），29.97 fps 素材 `t=0` 与 `t=0.34`
   是不同帧（`test_frame_seek`）。

### 6.2 必须用实际解码/动态测试才能回答的问题

1. **非整数帧率下路径 B 与路径 C 是否输出同一物理帧。** 仓库无 29.97 fps
   的跨历史同一性测试；ffmpeg 的关键帧 seek 可能先跳到更早的关键帧再解码，
   其与顺序管道在 `n/fps-1e-5` 处是否对齐，只能对真实文件逐帧比对
   （例如对 `media/smpte-2997.mp4` 分别用顺序读和重定位读全部逻辑号，做
   `np.array_equal` 矩阵）。
2. **VFR 文件的行为。** 全部分帧逻辑假设恒定 `fps`；VFR 文件的真实帧间隔
   与 `1/fps` 不符时，逻辑帧号与容器内实际帧的对应关系会系统性漂移，源码
   无任何 VFR 检测或补偿。
3. **真实 EOF 与 `n_frames` 估算的偏差方向与幅度**，以及尾部短读究竟在第几
   次读取发生——取决于编码器写出的完整帧数与容器 duration 字段。
4. **seek 回退 `-1e-5` 在高帧率/极端时间戳下是否足够。** epsilon 是固定
   10 µs，不随帧率缩放；帧率很高（帧间隔接近 epsilon 量级）或文件起始时间
   戳非零时，回退点可能越过相邻帧 PTS，需要实测验证。
5. **容器/编码组合差异**（长 GOP、B 帧重排、webm/vp8/vp9 强制解码器分支，
   ffmpeg_reader.py:133-138）对输出侧 `-ss` 精度的影响，均属 ffmpeg 运行时
   行为。
6. **重定位预读即短读时返回的旧帧在视觉上是否可接受**——源码只能确定它是
   重定位前的 `last_read`，其与目标时间的内容差距无源码信息。

### 6.3 建议的验证手段（不改变结论，仅供取证）

- 对 `media/smpte-2997.mp4` 构造两个 reader：一个按
  `t = n/fps` 顺序读全序列，另一个对每个 `n` 都用后退一步触发重定位，比对
  两组数组；
- 用 `pytest.warns` 捕获尾部警告序号，定位真实短读逻辑位并与
  `reader.n_frames` 比较；
- 若需覆盖 VFR，应先确认 ffmpeg 报告的 fps 与逐帧 `pts_time`
  （`ffprobe -show_frames`）是否一致，再解释任何差异。

---

## 附：关键代码位置索引

| 行为 | 位置 |
| --- | --- |
| 逻辑帧号公式与 `+1e-5` | `moviepy/video/io/ffmpeg_reader.py:271-277` |
| seek 时间戳回退 `-1e-5` 与双 `-ss` | `moviepy/video/io/ffmpeg_reader.py:99-126` |
| 三路径分支（缓存/跳过/重定位，100 帧阈值） | `moviepy/video/io/ffmpeg_reader.py:236-264` |
| `skip_frames` 不更新 `last_read` | `moviepy/video/io/ffmpeg_reader.py:171-178` |
| 短读警告、首次 `IOError`、末帧回退、`pos` 自增 | `moviepy/video/io/ffmpeg_reader.py:189-234` |
| 重定位保留 `last_read`（`delete_lastread=False`） | `moviepy/video/io/ffmpeg_reader.py:85-97, 279-289` |
| 帧率归一化 `x*1000/1001` | `moviepy/video/io/ffmpeg_reader.py:606-628` |
| `n_frames` 元数据估算 | `moviepy/video/io/ffmpeg_reader.py:725-734` |
| `VideoFileClip` 转发与 reader 生命周期 | `moviepy/video/io/VideoFileClip.py:113-144, 169-180` |
| `iter_frames` 的 `t=frame_index/fps` 网格 | `moviepy/Clip.py:516-571` |
| 顺序/乱序位置测试 | `tests/test_ffmpeg_reader.py:605-656` |
| 三历史同一性（整数帧率） | `tests/test_ffmpeg_reader.py:675-701` |
| 越界回退契约 | `tests/test_ffmpeg_reader.py:704-721` |
| 29.97 fps 仅有的取帧测试 | `tests/test_ffmpeg_reader.py:831-838` |
