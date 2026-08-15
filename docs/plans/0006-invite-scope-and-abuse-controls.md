# 0006 — 邀请码绑定相册 + 反滥用三件套（CSRF / captcha / 设备限流）

## 背景

MVP 是「一个全局邀请码，谁拿到都能搜全部相册」。流程跑通后暴露两类需求：

1. **权限收窄**：不同活动的照片发给不同的人，邀请码应当只解锁它对应的那一个相册。
2. **反滥用**：检索接口成本高（GPU 推理）且涉及人脸数据，需要 CSRF、captcha
   与设备级频控来抬高机器人批量调用的成本。

## 目标

1. 邀请码与相册一对一绑定；scope 不符的检索**在服务端硬性拒绝**（403），
   不是前端隐藏。
2. 保留「全相册码」的概念（站主自用），`.env` 里现有的 `INVITE_CODE_HASH`
   继续可用，语义即全相册 —— 升级不锁死任何人。
3. 登录必须通过 captcha；检索必须带 CSRF token；同一设备每小时最多 3 次检索
   （可配置）。
4. 上传校验维持现状 —— 该需求（≤10MB、仅 JPEG/PNG/WebP/HEIC、魔数嗅探不信
   Content-Type）在 `api/app/uploads.py` 已完整实现，本迭代不动。

## 数据模型（002_invite_code.sql，追加式）

```
invite_code
  id           uuidv7 PK
  prefix       VARCHAR(16) UNIQUE   -- 码的公开前半段，登录时用它定位行
  code_hash    TEXT                 -- argon2(秘密后半段)
  album        VARCHAR(200) NULL    -- NULL = 全相册（管理码）
  label        TEXT NULL            -- 发给谁/用途备注
  disabled_at  TIMESTAMPTZ NULL     -- 吊销即置位，行不删（留审计）
  created_at   TIMESTAMPTZ
```

**码的格式**：`<prefix>.<secret>`（8 位 hex + 24 位 urlsafe 随机）。
prefix 定位唯一一行 → 只做**一次** argon2 验证。不这样设计的话，登录要把库里
每一行 hash 都验一遍 —— argon2 单次约 50~100ms，码一多登录就被拖垮，
而且等于给爆破者送了一个放大器。

**兼容**：输入不含 `.` 的码走旧路径（`.env` 的 `INVITE_CODE_HASH`，全相册）。

## 鉴权与 scope 的贯穿

- JWT 增加 `alb` claim（string | null）。`require_session` 返回
  `SessionInfo(sid, album)`，取代裸 sid。
- **检索**：session 带 scope 时 —— 请求指定了别的相册 → `403 邀请码与所选相册不符`；
  未指定 → 服务端强制注入 scope。过滤发生在 SQL 参数层，前端只是展示。
- **/albums**：scoped session 只返回绑定的那一个相册。
- **/session/me**：返回 `album`，前端据此把相册选择器渲染成锁定态。

## 反滥用三件套

### CSRF：双提交 cookie

登录成功时下发**非 httponly** 的 `zrc_csrf` cookie（随机 32 hex）；
`POST /search`、`POST /session/logout` 必须带 `X-CSRF-Token` 头且与 cookie 相等
（`hmac.compare_digest`）。SameSite=lax 已经挡掉大部分跨站 POST，双提交是纵深。

### captcha：自研 SVG，零外部依赖

- 不用 reCAPTCHA/hCaptcha：前端 CSP 是 `default-src 'self'`，引第三方脚本要开洞，
  且把「谁在用人脸检索」告诉了第三方 —— 与本项目隐私立场冲突。
- `GET /session/captcha` → 4 位字符（去掉易混淆的 0O1I）渲染成扰动 SVG +
  HMAC 签名 token（内含 nonce 与 5 分钟过期）。**答案不出现在 token 里**，
  token 存的是 `HMAC(answer, nonce, exp)`，服务端无状态验证。
- 单次使用：进程内 used-nonce 集合（带 TTL 清理）。单实例 api 足够，
  与限流器同一假设，横向扩容时一并换 Redis。
- **只加在登录上**：登录是一切的门（session → 才能检索），而检索路径有
  session+CSRF+三层限流兜底。把 captcha 加在每次检索上会毁掉
  「三步完成」的核心流程（见 ui-ux skill），防护收益却是重复的。

### 设备限流：cookie 维度 3 次/小时

- 首次登录时下发 httponly 的 `zrc_device` cookie（随机 id，400 天）。
- 检索按 device id 限 `RATE_LIMIT_SEARCHES_PER_DEVICE_PER_HOUR`（默认 3）。
- **诚实说明**：清 cookie 即可换新设备身份，这层是「抬高普通滥用成本」，
  不是硬边界。硬边界仍是 IP（90/h）与 session（30/h）两层滑动窗口 ——
  三层全过才放行。device id 不入库，只存在于进程内限流窗口里。

## 运维：发码

```bash
docker compose --profile tools run --rm jobs python -m jobs invite create \
    --album 2026-08-12 --label "8月12日夜跑参与者"
# → 打印完整邀请码（仅此一次，库里只存 hash）
python -m jobs invite list / disable --prefix <prefix>
```

## 非范围

- 不做邀请码自助换发/过期策略（disabled_at 手动吊销够用）。
- 不做多相册码（一码多相册）。要发多场就发多张码。
- captcha 不上难度自适应；限流不上 Redis（单实例假设不变）。

## 验收标准

- [ ] scoped 码登录后：搜绑定相册 ✓；搜别的相册 403；不选相册时结果只来自绑定相册
- [ ] `.env` 旧码继续可登录且可搜全部相册
- [ ] 无 captcha / 答案错误 / token 过期 / token 重放 → 登录 4xx，文案友好
- [ ] 无 X-CSRF-Token 或与 cookie 不符 → /search 403
- [ ] 同设备一小时第 4 次检索 → 429 + Retry-After
- [ ] CI 全绿（含真库测试：建码 → prefix 查找 → 登录验证链路）

## 风险

1. **JWT 结构变更**：旧 session 的 token 没有 `alb` claim → 视为全相册还是失效？
   **选择失效**（缺 claim 按无效处理），部署后所有人重新登录一次。安全优先于便利。
2. **captcha 可访问性**：SVG 扰动文字对视障用户不友好。club 规模先接受，
   文案给出联系站主的兜底。
3. 进程内 nonce/限流状态在 api 重启时清零 —— 重启后短窗口内重放/超频可能放过，
   接受（与现有限流器同级别的既有取舍）。
