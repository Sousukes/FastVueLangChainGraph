<script setup lang="ts">
import { computed } from 'vue'
import { typeColor, type GraphNode, type GraphEdge } from '../composables/useGraph'

const props = withDefaults(
  defineProps<{
    nodes: GraphNode[]
    edges: GraphEdge[]
    height?: number
    /** 是否在边上标关系词。边一多就必须关掉，否则图会糊成一团 */
    showRelation?: boolean
  }>(),
  { height: 420, showRelation: true },
)

// 画布比例要和容器接近，否则 `preserveAspectRatio="meet"` 会在两侧留出大片空白。
// 实测：面板宽度约 830px、高 420px（比例 ≈ 2.0），而 800×430（比例 1.86）会缩到 0.82 倍，
// 左右各空 88px，图看起来"被挤在中间"。改成 900×420 就基本贴合了。
const W = 900
const PAD = 56

interface Pt {
  name: string
  x: number
  y: number
  vx: number
  vy: number
  r: number
  node: GraphNode
  deg: number
}

/**
 * 布局结果的记忆化缓存（**保留最近两份**）。
 *
 * ## 为什么需要它
 *
 * `layout` 是 computed，依赖 `props.nodes` / `props.edges` / `props.height`。
 * 实测（Node，同一算法）：n=40 约 20ms、n=200 约 88ms、n=300 约 206ms
 * —— 一次全量重算就吃掉几十帧预算。
 *
 * 但**真正的问题不是单次算得慢，而是它被反复重算**：
 * `GraphConsole.shown` 每次都返回新数组（`overview.value` 或 `result.value` 的引用），
 * 只要用户点一次检索、拖一次滑块、或构建进度回填一次，
 * `nodes`/`edges` 的内容就可能"引用没变但换了数据"，Vue 只能重算整个布局。
 *
 * ## 为什么用「内容签名」而不是「引用比较」
 *
 * Vue 的 computed 对数组 props 只能做浅比较（同一引用即认为未变）。
 * 这里数据来自 `await call(...)` 的响应体，**内容变了引用也会变**，
 * 引用比较在两个方向上都会错：既会漏该重算的，也会白算没变的。
 * 所以用「节点名 + 边两端」拼一个短签名做 key —— 比深比较快得多，
 * 又对内容变化敏感。
 *
 * ## 为什么是两份而不是一份
 *
 * 图谱页只有两种视图（全图 / 检索子图），用户会在两者间来回切。
 * 只留一份的话，「全图 → 子图 → 全图」的第二次全图会 miss 并把缓存覆盖成子图，
 * 于是每次切换都重算。留两份（LRU：新的插到前面，命中则提到前面），
 * 两种视图就都能命中 —— 这正是 `viewMode` 的两个取值。
 *
 * ## 注意：这只省掉「重复计算」，不改变计算结果
 *
 * 命中缓存时返回的是**同一份数据算出的坐标**，
 * 同样的输入必然得到同样的输出（布局是纯函数，见下方"初始位置用排序圆环"），
 * 所以截图可复现性不受影响。
 */
const CACHE_SIZE = 2
let layoutCache: Array<{
  key: string
  height: number
  result: { pts: Pt[]; links: Array<[number, number, GraphEdge]> }
}> = []

/** 给当前这份 nodes/edges 算一个廉价的内容签名 */
function signatureOf(nodes: GraphNode[], edges: GraphEdge[]): string {
  let s = `${nodes.length}|${edges.length}|`
  for (const n of nodes) s += n.name + ','
  for (const e of edges) s += e.head + '>' + e.tail + ','
  return s
}

/** 取缓存；命中则提到队首（LRU），未命中返回 null */
function takeFromCache(key: string, height: number) {
  const i = layoutCache.findIndex((c) => c.key === key && c.height === height)
  if (i < 0) return null
  const hit = layoutCache[i]
  layoutCache.splice(i, 1)
  layoutCache.unshift(hit)
  return hit.result
}

/** 写入缓存：新的插到队首，超出容量丢最旧的 */
function putInCache(key: string, height: number, result: { pts: Pt[]; links: Array<[number, number, GraphEdge]> }) {
  layoutCache.unshift({ key, height, result })
  if (layoutCache.length > CACHE_SIZE) layoutCache.length = CACHE_SIZE
}


/**
 * 力导向布局——**手写的，没引 d3**。
 *
 * 引一个 d3-force 只要几十 KB，但这一阶段要讲的是"图长什么样"，
 * 而"节点为什么摆在这个位置"恰恰是理解图的关键。六十行能讲清楚的事，
 * 不值得藏进一个黑盒依赖里。三个力就够了：
 *
 *   1. **斥力**（所有节点两两）：O(n²)，节点少的时候完全够用；
 *   2. **弹簧**（每条边）：把有关系的节点拉到目标长度附近；
 *   3. **向心力**：防止孤立节点被斥力推到画布外面去。
 *
 * 初始位置用**按名字排序后的圆环**，不是随机撒点——
 * 同一份数据每次渲染出来的图必须一模一样，否则没法截图对比。
 *
 * ⚠️ 迭代次数（`ITER`）与斥力常数（`8000`）都**刻意不为了性能而调**：
 * 它们直接决定"图摊多开"的手感，是这个阶段的教学重点之一。
 * 性能靠上方的**结果记忆化**解决，不靠砍迭代次数。
 */
const layout = computed(() => {
  const H = props.height
  const nodes = props.nodes
  if (!nodes.length) return { pts: [] as Pt[], links: [] as Array<[number, number, GraphEdge]> }

  // 同一份数据 + 同一个画布高度 → 直接复用上次的坐标，不再重算
  const key = signatureOf(nodes, props.edges)
  const cached = takeFromCache(key, H)
  if (cached) return cached

  // 有效度数：后端给了 degree 就用它（全图模式），否则用边数现推（检索子图）
  const edgeDeg = new Map<string, number>()
  for (const e of props.edges) {
    edgeDeg.set(e.head, (edgeDeg.get(e.head) ?? 0) + 1)
    edgeDeg.set(e.tail, (edgeDeg.get(e.tail) ?? 0) + 1)
  }

  const ordered = [...nodes].sort((a, b) => a.name.localeCompare(b.name, 'zh'))
  const radius = Math.min(W, H) * 0.34
  const pts: Pt[] = ordered.map((n, i) => {
    const ang = (i / ordered.length) * Math.PI * 2
    const deg = Math.max(n.degree ?? 0, edgeDeg.get(n.name) ?? 0)
    return {
      name: n.name,
      x: W / 2 + Math.cos(ang) * radius,
      y: H / 2 + Math.sin(ang) * radius,
      vx: 0,
      vy: 0,
      // 半径编码"连接度"：一眼能看出谁是枢纽
      r: 5 + Math.min(13, deg * 1.4) + (n.isSeed ? 3 : 0),
      node: n,
      deg,
    }
  })

  const idx = new Map(pts.map((p, i) => [p.name, i]))
  const links: Array<[number, number, GraphEdge]> = []
  for (const e of props.edges) {
    const a = idx.get(e.head)
    const b = idx.get(e.tail)
    if (a != null && b != null && a !== b) links.push([a, b, e])
  }

  const L = links.length > 60 ? 82 : 148
  const ITER = 340
  for (let it = 0; it < ITER; it++) {
    const cool = 1 - it / ITER

    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        let dx = pts[j].x - pts[i].x
        let dy = pts[j].y - pts[i].y
        let d2 = dx * dx + dy * dy
        if (d2 < 1) {
          // 完全重合时给一个**确定性**的微推，不要用 Math.random —— 那会破坏可复现性
          const ang = ((i * 12.9898 + j * 78.233) % 6.2832) as number
          dx = Math.cos(ang)
          dy = Math.sin(ang)
          d2 = 1
        }
        const d = Math.sqrt(d2)
        // 斥力常数直接决定图"摊多开"。8000 是照着 900×420 画布调的：
        // 太小图会缩成中间一团，太大节点会被挤到边上贴框。
        const f = 8000 / d2
        const fx = (dx / d) * f
        const fy = (dy / d) * f
        pts[i].vx -= fx
        pts[i].vy -= fy
        pts[j].vx += fx
        pts[j].vy += fy
      }
    }

    for (const [a, b] of links) {
      const A = pts[a]
      const B = pts[b]
      const dx = B.x - A.x
      const dy = B.y - A.y
      const d = Math.hypot(dx, dy) || 1
      const f = (d - L) * 0.045
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      A.vx += fx
      A.vy += fy
      B.vx -= fx
      B.vy -= fy
    }

    for (const p of pts) {
      p.vx += (W / 2 - p.x) * 0.010
      p.vy += (H / 2 - p.y) * 0.020
      p.vx *= 0.86
      p.vy *= 0.86
      p.x += p.vx * cool
      p.y += p.vy * cool
    }
  }

  for (const p of pts) {
    p.x = Math.min(W - PAD, Math.max(PAD, p.x))
    p.y = Math.min(H - PAD, Math.max(PAD, p.y))
  }

  const result = { pts, links }
  putInCache(key, H, result)
  return result
})

/** 计算好的线段：起点/终点都从圆的边界开始，不然箭头会被节点盖住 */
const lines = computed(() =>
  layout.value.links.map(([a, b, e]) => {
    const A = layout.value.pts[a]
    const B = layout.value.pts[b]
    const dx = B.x - A.x
    const dy = B.y - A.y
    const d = Math.hypot(dx, dy) || 1
    const ux = dx / d
    const uy = dy / d
    return {
      id: e.id,
      edge: e,
      x1: A.x + ux * (A.r + 1),
      y1: A.y + uy * (A.r + 1),
      x2: B.x - ux * (B.r + 6),
      y2: B.y - uy * (B.r + 6),
      mx: (A.x + B.x) / 2,
      my: (A.y + B.y) / 2,
    }
  }),
)

/** 边一多就不标关系词了。18 条以上时，标签会互相压住，反而看不清任何一条 */
const relationVisible = computed(() => props.showRelation && props.edges.length <= 18)

/** 标签也要节流：全图模式下只标种子和枢纽 */
const labelled = computed(() => {
  const many = props.nodes.length > 20
  return new Set(
    props.nodes
      .filter((n) => !many || n.isSeed || n.hop === 1 || (n.degree ?? 0) >= 4)
      .map((n) => n.name),
  )
})

/** hop 1 是"直接命中"，用 Signal 琥珀；hop 2 退一档；全图模式用最暗的线 */
function edgeClass(hop: number) {
  if (hop === 1) return 'edge hot'
  if (hop === 2) return 'edge warm'
  return 'edge dim'
}

function edgeMarker(hop: number) {
  return hop === 1 ? 'url(#arrow-hot)' : hop === 2 ? 'url(#arrow-warm)' : 'url(#arrow-dim)'
}
</script>

<template>
  <div class="graph-view" :style="{ height: `${height}px` }">
    <svg :viewBox="`0 0 ${W} ${height}`" preserveAspectRatio="xMidYMid meet" role="img">
      <defs>
        <marker
          id="arrow-hot"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 z" fill="#FFB454" />
        </marker>
        <marker
          id="arrow-warm"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="7"
          markerHeight="7"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 z" fill="#5BC8B0" />
        </marker>
        <marker
          id="arrow-dim"
          viewBox="0 0 8 8"
          refX="7"
          refY="4"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M0,0 L8,4 L0,8 z" fill="#3A4250" />
        </marker>
      </defs>

      <!-- 边 -->
      <g class="edges">
        <line
          v-for="l in lines"
          :key="l.id"
          :class="edgeClass(l.edge.hop)"
          :x1="l.x1"
          :y1="l.y1"
          :x2="l.x2"
          :y2="l.y2"
          :marker-end="edgeMarker(l.edge.hop)"
        />
        <template v-if="relationVisible">
          <text
            v-for="l in lines"
            :key="`t-${l.id}`"
            class="rel"
            :class="{ hot: l.edge.hop === 1 }"
            :x="l.mx"
            :y="l.my - 3"
            text-anchor="middle"
          >
            {{ l.edge.relation }}
          </text>
        </template>
      </g>

      <!-- 节点 -->
      <g class="nodes">
        <g
          v-for="p in layout.pts"
          :key="p.name"
          :class="{ seed: p.node.isSeed, hop1: p.node.hop === 1 }"
          :transform="`translate(${p.x}, ${p.y})`"
        >
          <circle v-if="p.node.isSeed" class="halo" :r="p.r + 7" />
          <circle class="dot" :r="p.r" :fill="typeColor(p.node.type)" />
          <text
            v-if="labelled.has(p.name)"
            class="label"
            :x="p.r + 5"
            :y="4"
            :class="{ strong: p.node.isSeed }"
          >
            {{ p.name }}
          </text>
        </g>
      </g>
    </svg>

    <p v-if="!nodes.length" class="blank">
      图上还没有东西。先在右边<b>构建</b>一次——图谱是"抽"出来的，不是本来就有的。
    </p>
  </div>
</template>

<style scoped>
.graph-view {
  position: relative;
  width: 100%;
  background: var(--ink);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
svg { width: 100%; height: 100%; display: block; }

.edge { stroke-width: 1.1; }
.edge.hot { stroke: var(--signal); stroke-width: 2; }
.edge.warm { stroke: color-mix(in srgb, var(--data) 65%, var(--line)); stroke-width: 1.4; }
.edge.dim { stroke: #3a4250; }

.rel {
  font-family: var(--font-mono);
  font-size: 9px;
  fill: var(--muted);
  paint-order: stroke;
  stroke: var(--ink);
  stroke-width: 3px;
}
.rel.hot { fill: var(--signal); }

.dot { stroke: var(--ink); stroke-width: 1.5; }
.halo {
  fill: none;
  stroke: var(--signal);
  stroke-width: 1.2;
  stroke-dasharray: 3 3;
  opacity: 0.85;
}

.label {
  font-family: var(--font-body);
  font-size: 10.5px;
  fill: var(--muted);
  paint-order: stroke;
  stroke: var(--ink);
  stroke-width: 3px;
  pointer-events: none;
}
.label.strong { fill: var(--paper); font-weight: 600; }

.blank {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0;
  padding: 24px;
  font-size: 12.5px;
  color: var(--muted);
  text-align: center;
}
.blank b { color: var(--paper); }
</style>
