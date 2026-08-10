---
name: design-system
description: 本项目前端的设计系统规范 —— 颜色 / 间距 / 字号 / 圆角 token，组件用法约定，深色主题规则。在 web/ 下新增或修改任何组件、样式、Tailwind class 时使用；也用于审查 PR 里的样式是否偏离系统。
---

# 设计系统

单一来源是 `web/src/index.css` 的 `@theme` 块（Tailwind v4 的 CSS-first 配置）。
组件里**只用语义 token，不写字面色值**。看到 `bg-[#1a1a1f]` 或 `text-gray-400` 就是违规。

## 设计立场

照片是主角，界面必须退后。这条决定了下面几乎所有取舍：

- 深色底（`ink-950`），让照片在视觉上跳出来，手机上也更省电。
- 强调色（`accent`）**只用于「开始检索」这一个主动作**。到处都是强调色等于没有强调色。
- 不用阴影做层级，用背景色阶（`ink-900` / `ink-800`）。深色主题里阴影几乎不可见。
- 装饰性动效一律不加。只保留状态反馈（hover / focus / disabled）。

## 颜色 token

| Token | 用途 |
| --- | --- |
| `ink-950` | 页面底色 |
| `ink-900` | 卡片 / 次级按钮 / 图片占位底 |
| `ink-800` | 边框、分隔线、disabled 背景 |
| `ink-600` | 弱化文字（计数、时间戳、次要提示） |
| `ink-400` | 说明性正文（隐私告知、提示文案） |
| `ink-200` / `ink-100` | 正文 / 主标题 |
| `accent-500` | 主动作按钮底色（hover 用 `accent-600`） |
| `accent-400` | 焦点环 |
| `danger-500` | 错误文字、危险操作 hover |
| `warn-500` | 警示（当前未使用，新增前先确认真的需要） |

新增颜色前先问：能不能用现有色阶表达？大多数情况能。

## 间距与排版

- 间距只用 Tailwind 默认阶（`1.5` / `3` / `4` / `5` / `8` / `10`）。不要出现 `gap-[7px]`。
- 圆角：卡片与按钮用 `rounded-xl`，缩略图用 `rounded-lg`，头像/图标按钮用 `rounded-full`。
- 字号：标题 `text-lg`~`text-2xl` + `font-semibold` + `tracking-tight`；正文 `text-sm`；
  辅助说明 `text-[13px]`；元信息 `text-xs`。
- 中文正文用 `leading-relaxed` —— 中文字面积大，默认行高偏挤。

## 组件约定

**按钮**三种，不要发明第四种：

```
主动作   bg-accent-500 hover:bg-accent-600 rounded-xl px-4 py-3 font-medium
次动作   bg-ink-900 hover:bg-ink-800 rounded-xl px-4 py-3 text-sm font-medium
文字链   text-ink-600 hover:text-ink-200 text-xs
```

所有 disabled 态统一：`disabled:bg-ink-800 disabled:text-ink-600 disabled:cursor-not-allowed`。

**输入框**：`bg-ink-900 border border-ink-800 focus:border-accent-500 rounded-xl px-4 py-3 text-base`。
`text-base`（16px）不是随意选的 —— iOS Safari 会对小于 16px 的输入框自动缩放页面。

**图片**：一律给 `aspect-*` 固定比例 + `object-cover`，避免加载完成时布局跳动。
列表里的图必须 `loading="lazy" decoding="async"`。

## 硬性规则

1. **焦点环不许移除。** `:focus-visible` 在 `index.css` 里全局定义，不要用 `outline-none` 盖掉。
2. **可点区域最小 44×44px**（移动端触摸目标）。小图标按钮用 `size-9` 或加 padding 撑够。
3. **`prefers-reduced-motion` 已全局处理**，新增动画不需要单独适配，但也不要用 `!important` 绕过。
4. **文字对比度** 至少 4.5:1。`ink-600` 已经是可用下限，不要在它之上再叠透明度。
5. **不引入 UI 组件库、图标库或字体文件。** 部署环境的 CSP 禁止外部资源，
   而且这个项目只有一个页面，引库的收益远小于成本。

## 检查清单

改完样式后逐项过一遍：

- [ ] 没有字面色值 / 魔法间距
- [ ] 深浅层级靠背景色阶而非阴影
- [ ] 强调色只出现在主动作上
- [ ] 触摸目标 ≥ 44px
- [ ] 焦点态可见
- [ ] 375px 宽（iPhone SE）不横向溢出
- [ ] 图片有固定宽高比，长列表懒加载
