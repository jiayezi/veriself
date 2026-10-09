-- veriself 星型模型 DDL —— IFACE-v1 契约第 1 节（冻结）
--
-- **唯一 DDL 来源**：其他任何模块禁止内联 DDL（契约第 7 节铁律 3）。
-- 9 张表全部幂等建表（IF NOT EXISTS），`loader.ensure_schema` 可重复调用。
-- 列名/类型/主键与 `docs/00-接口契约.md` 第 1 节逐字一致，不得增删列。

-- ---------------------------------------------------------------- 维度表
CREATE TABLE IF NOT EXISTS dim_date (
    date_key      INTEGER PRIMARY KEY,      -- yyyymmdd
    date          DATE NOT NULL,
    year          INTEGER,
    quarter       INTEGER,
    month         INTEGER,
    week          INTEGER,
    day_of_week   INTEGER,                  -- 1=周一 ... 7=周日
    weekday_name  VARCHAR,                  -- Monday..Sunday
    is_weekend    BOOLEAN,
    is_holiday    BOOLEAN                   -- 合成数据只覆盖 CN 法定节假日近似
);

CREATE TABLE IF NOT EXISTS dim_subject (    -- SCD2
    subject_sk      BIGINT PRIMARY KEY,     -- 代理键
    subject_id      VARCHAR NOT NULL,       -- 业务键，如 'S001'
    name            VARCHAR,
    birth_date      DATE,
    sleep_need_h    DOUBLE,                 -- 个体睡眠需求（小时）
    base_weight_kg  DOUBLE,
    timezone        VARCHAR,
    valid_from      TIMESTAMP NOT NULL,
    valid_to        TIMESTAMP,              -- NULL = 当前
    is_current      BOOLEAN NOT NULL,
    version         INTEGER NOT NULL,
    recorded_at     TIMESTAMP NOT NULL,     -- 记录时间（双时间轴另一半）
    -- 区间必须非空且正向：`valid_to <= valid_from` 的行是"负长度区间"，SCD2 无意义
    CONSTRAINT chk_subject_valid_range CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- SCD2 最根本的约束：同一主体的一个版本只能有一个起点。
-- 两个版本共享 `valid_from` 时"哪一个权威"无解，as-of 查询会同时命中两行。
-- 注：DuckDB **不支持部分索引**（`... WHERE is_current` 会抛 NotImplementedException），
-- 因此"每主体至多一行 is_current = true"无法用索引表达，只能由应用层维护 +
-- 数据质量测试守住。
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_subject_version
    ON dim_subject (subject_id, valid_from);

CREATE TABLE IF NOT EXISTS dim_source (
    source_id        VARCHAR PRIMARY KEY,   -- 'wearable' | 'phone' | 'bank' | 'llm_client'
    display_name     VARCHAR,
    reliability_tier VARCHAR                -- 'high' | 'medium' | 'low'
);

-- 口径定义表：当前版（metrics/*.yml）与历史版（metrics/history/）各一行。
-- 主键是 (metric_id, version)。只按 metric_id 连接会把多个口径版本乘到事实行上。
-- 读取某行指标值的 grain 时必须同时匹配版本：
--   dim_metric.metric_id = fact_metric_value.metric_id
--   AND dim_metric.version = fact_metric_value.metric_version
CREATE TABLE IF NOT EXISTS dim_metric (
    metric_id     VARCHAR NOT NULL,         -- 如 'subject.sleep_debt_7d'
    display_name  VARCHAR,
    unit          VARCHAR,
    direction     VARCHAR,                  -- higher_better | lower_better | neutral
    grain         VARCHAR,
    version       INTEGER NOT NULL,
    contract_hash VARCHAR,
    status        VARCHAR,
    PRIMARY KEY (metric_id, version)
);

-- ---------------------------------------------------------------- 事实表
-- 观测：**业务自然键作主键** `(subject_id, observed_at, channel)`。
-- 为什么不用代理键 `observation_id`：观测没有天然身份，同一主体同一通道同一时刻
-- 只应有一条读数；用自然键才能让"重复插入"被数据库拒绝，而不是被静默接受。
-- `obs_daily` 视图用的是 `sum(value) FILTER (WHERE channel = ...)`——
-- 一条重复观测会让当日数值**翻倍且无任何报错**。
-- `observation_id` 保留为普通列 + 唯一索引，仅供 provenance 引用/调试。
CREATE TABLE IF NOT EXISTS fact_observation (   -- 粒度：subject × 时刻 × 通道
    observation_id BIGINT NOT NULL,
    subject_id     VARCHAR NOT NULL,
    observed_at    TIMESTAMP NOT NULL,
    date_key       INTEGER NOT NULL,
    channel        VARCHAR NOT NULL,            -- 10 个日粒度通道 + heart_rate（见 docs/00 §2）
    value          DOUBLE NOT NULL,
    source_id      VARCHAR NOT NULL,
    recorded_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (subject_id, observed_at, channel)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_observation_id
    ON fact_observation (observation_id);
-- 自然键的唯一性**同时用索引声明一次**：
-- `CREATE TABLE IF NOT EXISTS` 遇到旧表会静默跳过（主键改不了），
-- 但索引是独立语句、对任何库都会执行，所以存量库也能拿到这层保护。
-- 若存量库已有重复观测，这里会**报错**而不是静默通过。
CREATE UNIQUE INDEX IF NOT EXISTS ux_fact_observation_natural_key
    ON fact_observation (subject_id, observed_at, channel);

-- 事件：**刻意不加自然键唯一约束**。`(subject_id, occurred_at, event_type)`
-- 会拒绝合法数据——同一分钟的两笔消费是正常的（金额/分类都不同），
-- 而 `occurred_at` 只精确到分钟。事件的真身份须由上游提供（`source_event_id`），
-- 当前数据源没有，故如实留空而不是硬凑一个"看起来唯一"的键。
CREATE TABLE IF NOT EXISTS fact_event (         -- 粒度：事件
    event_id    BIGINT PRIMARY KEY,
    subject_id  VARCHAR NOT NULL,
    occurred_at TIMESTAMP NOT NULL,
    date_key    INTEGER NOT NULL,
    event_type  VARCHAR NOT NULL,               -- transaction | note | llm_turn | workout
    -- ⚠️ `amount` / `category` 的**单位随 event_type 变化**（这是"事件表"的固有形态：
    --    类型决定哪些列有意义，其余列为 NULL）：
    --      transaction → amount 是金额，category 是消费类别
    --      workout     → amount 是**运动时长（分钟）**，category 是运动种类
    --    当前只有 transaction 路径被指标消费（evt_daily.spending /
    --    discretionary_spending）；**workout 的时长没有任何指标在用**。
    amount      DOUBLE,                         -- transaction: 金额 ｜ workout: 分钟（见上）
    category    VARCHAR,                        -- transaction: 消费类别 ｜ workout: 运动种类
    text        VARCHAR,                        -- note | llm_turn 用
    source_id   VARCHAR NOT NULL,
    recorded_at TIMESTAMP NOT NULL
);

-- 日情境是事实，不是维度：一行是「某主体某一天」的出差/生病/地点。
-- 与 fact_metric_value 连接必须同时用 subject_id 和 date_key。
-- 只按 date_key 连接会在多主体时把同一天的情境乘到每个主体上。
CREATE TABLE IF NOT EXISTS fact_subject_day (  -- 粒度：subject × date
    subject_id    VARCHAR NOT NULL,
    date_key      INTEGER NOT NULL,            -- yyyymmdd
    is_travel     BOOLEAN NOT NULL,
    is_illness    BOOLEAN NOT NULL,
    location_type VARCHAR NOT NULL,            -- 'home' | 'office' | 'other'
    PRIMARY KEY (subject_id, date_key)
);

CREATE TABLE IF NOT EXISTS fact_metric_value (  -- 粒度：metric × subject × date（物化结果，双时间轴）
    metric_id      VARCHAR NOT NULL,
    subject_id     VARCHAR NOT NULL,
    date_key       INTEGER NOT NULL,
    value          DOUBLE NOT NULL,
    metric_version INTEGER NOT NULL,
    contract_hash  VARCHAR NOT NULL,
    computed_at    TIMESTAMP NOT NULL,
    valid_from     TIMESTAMP NOT NULL,
    valid_to       TIMESTAMP,                   -- NULL = 有效
    PRIMARY KEY (metric_id, subject_id, date_key, valid_from)
);

CREATE TABLE IF NOT EXISTS fact_audit_log (
    audit_id        BIGINT PRIMARY KEY,
    queried_at      TIMESTAMP NOT NULL,
    actor_role      VARCHAR NOT NULL,           -- owner | partner | researcher
    request_json    VARCHAR NOT NULL,
    compiled_sql    VARCHAR,
    metric_versions VARCHAR,
    contract_hashes VARCHAR,
    rls_applied     VARCHAR,
    checks_passed   VARCHAR,
    outcome         VARCHAR NOT NULL            -- ok | rejected:<rule>
);
