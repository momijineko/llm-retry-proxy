"""配置中心页面结构测试：settings.html 关键交互与三页导航入口"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "settings.html").read_text(encoding="utf-8")

    def test_page_has_nav_and_save_actions(self):
        self.assertIn('<a href="/key-pools">号池</a><a class="active" href="/settings" aria-current="page">配置</a>', self.html)
        self.assertIn('id="saveBtn"', self.html)
        self.assertIn('id="resetAll"', self.html)
        self.assertIn('id="persistBanner"', self.html)

    def test_renders_apply_badges(self):
        self.assertIn('<span class="pill ok">立即生效</span>', self.html)
        self.assertIn('<span class="pill warn">重启后生效</span>', self.html)
        self.assertIn('<span class="pill red">重建镜像后生效</span>', self.html)
        self.assertIn('<span class="pill danger">敏感</span>', self.html)

    def test_controls_cover_all_types(self):
        self.assertIn("it.type==='bool'", self.html)
        self.assertIn("it.type==='enum'", self.html)
        self.assertIn("it.type==='int'", self.html)
        self.assertIn("it.type==='float'", self.html)
        self.assertIn("it.type==='int'||it.type==='float'?'number':'text'", self.html)

    def test_all_editable_controls_carry_data_key(self):
        # 输入框与下拉框必须带 data-key，否则 bindEvents 的
        # input[data-key]/select[data-key] 选择器匹配不到，修改无法标记为已修改
        self.assertIn('<input type="${type}" id="ctrl-${it.key}" data-key="${it.key}"', self.html)
        self.assertIn('<select id="ctrl-${it.key}" data-key="${it.key}"', self.html)
        self.assertIn('<label class="switch" id="ctrl-${it.key}" data-key="${it.key}"', self.html)

    def test_save_posts_updates_and_remove(self):
        self.assertIn("body:JSON.stringify({updates,remove})", self.html)
        self.assertIn("res.need_restart&&res.need_restart.length", self.html)
        self.assertIn("res.removed&&res.removed.length", self.html)
        self.assertIn("未持久化", self.html)

    def test_unpersisted_restart_items_warn_instead_of_memory_claim(self):
        # 无 .env 文件时重启后生效项什么都没保存，提示必须区分于"仅内存生效"
        self.assertIn("重启后生效项保存无效，请修改宿主机 .env 或 compose 环境变量后重建容器", self.html)
        self.assertIn("仅内存生效，重启后丢失", self.html)

    def test_reset_button_marks_key_as_remove(self):
        self.assertIn("DIRTY[key]=''", self.html)
        self.assertIn("data-reset", self.html)
        self.assertIn("恢复默认值", self.html)

    def test_reset_all_populates_reset_keys(self):
        # "全部重置"必须登记 RESET_KEYS，否则 save() 的敏感项守卫会静默跳过敏感项
        self.assertIn("RESET_KEYS=new Set()", self.html)
        self.assertIn("RESET_KEYS.add(it.key)", self.html)

    def test_reset_button_is_compact_not_stretched(self):
        # 重置按钮宽度固定为内容宽度，剩余空间全部留给输入框
        self.assertIn(".item-control .row .reset{flex:0 0 auto;width:auto}", self.html)
        self.assertIn('class="reset"', self.html)

    def test_secret_input_does_not_echo_plaintext(self):
        self.assertIn("if(it.secret)return ''", self.html)
        self.assertIn("已配置（留空不修改，输入新值覆盖）", self.html)

    def test_provider_aliases_uses_multi_row_editor(self):
        # Provider 显示别名以多行两列编辑：行输入无 data-key（避免与普通控件混淆），
        # 增删行与输入均走 serializeRowValue 拼接，保存时提交 from:to,from:to
        self.assertIn("ROW_EDITORS[it.key]", self.html)
        self.assertIn('class="alias-editor"', self.html)
        self.assertIn("PROVIDER_ALIASES: {itemSep: ':'", self.html)
        self.assertIn("cls: 'row-from'", self.html)
        self.assertIn("cls: 'row-to'", self.html)
        self.assertIn('class="alias-del"', self.html)
        self.assertIn('class="alias-add"', self.html)
        self.assertIn("function parseRowValue", self.html)
        self.assertIn("function serializeRowValue", self.html)
        self.assertIn("syncAliasEditor(editor,key)", self.html)

    def test_extra_upstreams_uses_multi_row_editor(self):
        # 额外上游路由同样使用多行编辑器：三列（前缀|上游地址|供应商），保存时拼接 prefix|url|provider
        self.assertIn("EXTRA_UPSTREAMS: {itemSep: '|'", self.html)
        self.assertIn("cls: 'row-prefix'", self.html)
        self.assertIn("cls: 'row-url'", self.html)
        self.assertIn("cls: 'row-provider'", self.html)

    def test_key_pools_uses_multi_row_editor_with_secret_semantics(self):
        # 号池 Key 列表使用三列编辑器（上游地址|供应商|Key 列表）；secret 项不回显明文，
        # 仅提供空行输入并提示"留空不修改"
        self.assertIn("KEY_POOLS: {itemSep: '|'", self.html)
        self.assertIn("cls: 'row-keys'", self.html)
        self.assertIn("parseEntry: e => e.includes('|')", self.html)
        self.assertIn("serializeRow: r =>", self.html)
        self.assertIn("it.secret ? [{}] : parseRowValue(val, cfg)", self.html)
        self.assertIn("已配置（留空不修改，输入新值覆盖）", self.html)
        self.assertIn("KEY_POOLS 的上游地址与供应商必须成对填写", self.html)

    def test_row_editors_save_validates_required_and_separators(self):
        # 保存前校验每行必填字段、字段不含逗号或组内分隔符（否则拼接后无法被后端解析）
        self.assertIn("必须填写", self.html)
        self.assertIn("不能包含逗号或", self.html)
        self.assertIn("前缀只能是路径（如 /aihub）", self.html)

    def test_hot_items_omit_current_effect_text(self):
        # 立即生效项的输入框即当前生效值，"当前生效"冗余；仅在与默认不同时提示默认
        self.assertIn("it.apply==='hot'", self.html)
        self.assertIn("eff!==def ? '默认: '+esc(def||'（空）') : ''", self.html)

    def test_row_editors_show_entry_count_instead_of_raw_string(self):
        # 行编辑器项的"当前生效"以条数呈现，不再重复展示拼接字符串
        self.assertIn("parseRowValue(it.effective_value, ROW_EDITORS[it.key]).length", self.html)
        self.assertIn("count + ' 条'", self.html)

    def test_tz_is_a_regular_card(self):
        # 容器时区作为普通卡片参与两列网格（不再铺满整行）
        self.assertNotIn("' item-full'", self.html)
        self.assertNotIn(".item-full{", self.html)

    def test_enum_selects_use_chinese_labels(self):
        # 竞速模式/防护模式下拉显示中文，option 值保持存储值以便原样提交
        self.assertIn("const ENUM_LABELS = {", self.html)
        self.assertIn("HEDGE_MODE: {off: '串行重试', race: '每轮并发竞速', stagger: '交错补发（会增加上游压力）'}", self.html)
        self.assertIn("DLP_MODE: {off: '关闭', audit: '仅告警', redact: '脱敏后转发', block: '拦截'}", self.html)
        self.assertIn("esc(labels[e]||e)", self.html)

    def test_binary_toggle_uses_switch(self):
        # 二值"开启/关闭"选项（桥接开关）渲染为拨动开关，值映射为存储值（bridge/off）
        self.assertIn("const TOGGLE_KEYS = {", self.html)
        self.assertIn("SSE2WS_MODE: {on: 'bridge', off: 'off'}", self.html)
        self.assertIn('class="switch"', self.html)
        self.assertIn('class="slider"', self.html)

    def test_bool_items_use_switch(self):
        # bool 开关项统一渲染为拨动开关，值保持 true/false（data-bool 区分于 enum 映射值）
        self.assertIn('data-bool="1"', self.html)
        self.assertIn("DIRTY[key]=isBool?(cb.checked?'true':'false'):(cb.checked?cfg.on:cfg.off)", self.html)
        self.assertNotIn('class="check"', self.html)
        # 旧 checkbox UI 的死代码引用必须清除（closest('.check') 在 .switch 结构下永不匹配）
        self.assertNotIn("closest('.check')", self.html)

    def test_input_falls_back_to_effective_value_without_env_file(self):
        # 容器模式无 .env 文件时，输入框回退显示当前生效值
        self.assertIn("return it.file_value||it.effective_value", self.html)

    def test_rebuild_items_are_editable(self):
        # 构建期配置可编辑（写入 .env 后重建镜像生效），不再有 disabled 逻辑
        self.assertNotIn("it.apply==='rebuild'?'disabled'", self.html)
        self.assertNotIn("it.apply==='rebuild'?'':", self.html)
        self.assertIn("重建镜像后生效", self.html)

    def test_items_layout_two_columns_with_narrow_fallback(self):
        # 卡片内配置项默认一行两列；窄屏（820px 以下）回退为一行一列
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", self.html)
        self.assertIn(".group-head{grid-column:1/-1", self.html)
        self.assertIn("@media(max-width:820px)", self.html)
        self.assertIn(".group{grid-template-columns:1fr}", self.html)

    def test_item_title_uses_chinese_name(self):
        # 主标题为中文名，徽标紧随其后
        self.assertIn("esc(it.name||it.key)", self.html)
        self.assertIn('class="cname"', self.html)
        self.assertIn('class="pill', self.html)

    def test_key_label_precedes_description(self):
        # key 标签（如 DOCKER_REGISTRY）置于描述信息首位，再展示描述内容；无描述时仍展示 key 标签
        self.assertIn('<div class="desc"><code class="env-name">${esc(it.key)}</code>', self.html)
        self.assertNotIn('class="env-name">${esc(it.key)}</code> ${pills}', self.html)

    def test_build_only_items_render_as_group_description(self):
        # 构建期配置不作为独立卡片，渲染为分组描述信息区
        self.assertIn("items.filter(it=>it.hidden)", self.html)
        self.assertIn("items.filter(it=>!it.hidden)", self.html)
        self.assertIn('class="group-meta"', self.html)
        self.assertIn("构建期配置（修改需重建镜像）", self.html)
        self.assertIn("items.length-metaItems.length", self.html)

    def test_side_nav_layout_with_scroll_spy(self):
        # 左侧快速导航 + 滚动高亮（参照统计页面模式），无返回顶部与标签头
        self.assertIn('class="dashboard-layout"', self.html)
        self.assertIn('class="side-nav"', self.html)
        self.assertIn("grid-template-columns:200px minmax(0,1fr)", self.html)
        self.assertIn("max-width:1720px", self.html)
        self.assertIn("@media(max-width:1180px)", self.html)
        self.assertIn('data-target="g${gi}"', self.html)
        self.assertIn("new IntersectionObserver", self.html)
        self.assertIn("rootMargin:'-18% 0px -70% 0px'", self.html)
        self.assertNotIn("nav-top", self.html)
        self.assertNotIn("返回顶部", self.html)
        self.assertNotIn("nav-label", self.html)
        self.assertNotIn("快速导航", self.html)

    def test_banner_sits_inside_right_content_column(self):
        # 顶部提示条不占左侧导航栏位置，位于右侧内容列顶部
        self.assertIn('<div class="settings-main">', self.html)
        self.assertLess(
            self.html.index('class="side-nav"'),
            self.html.index('id="persistBanner"'),
        )
        self.assertLess(
            self.html.index('id="persistBanner"'),
            self.html.index('id="content"'),
        )

    def test_side_nav_groups_cover_all_settings_groups(self):
        # 侧栏二次分组必须覆盖全部 14 个配置分组，防止新增分组未接入导航
        for name in ('Docker 与运行环境', '服务与访问控制', '上游、路由与网络', '日志',
                     'Codex Responses WebSocket 桥接 (SSE2WS)', '连接与响应超时',
                     '重试与退避', '竞速模式', '号池来源与鉴权', '号池熔断与选择',
                     '在线同步', 'Token 统计', '上游兼容', '请求正文敏感信息防护'):
            self.assertIn(name, self.html, name)

    def test_split_ip_editors_declared(self):
        # IP/CIDR 双组编辑器仅覆盖 TRUSTED_PROXY_IPS 与 IP_AUTO_BAN_EXEMPT 两个键，
        # 不得误拆其他配置项（如 IP_BLACKLIST 保持普通单行输入）
        self.assertIn('const SPLIT_EDITORS = {', self.html, 'settings.html 缺少 SPLIT_EDITORS 声明')
        self.assertIn('TRUSTED_PROXY_IPS: {label4: \'IPv4Network\', label6: \'IPv6Network\'}', self.html,
                      'SPLIT_EDITORS 缺少 TRUSTED_PROXY_IPS 键')
        self.assertIn('IP_AUTO_BAN_EXEMPT: {label4: \'IPv4Network\', label6: \'IPv6Network\'}', self.html,
                      'SPLIT_EDITORS 缺少 IP_AUTO_BAN_EXEMPT 键')
        self.assertNotIn('IP_BLACKLIST', self.html.split('const SPLIT_EDITORS', 1)[1], 'IP_BLACKLIST 不应被拆分为双组编辑器')

    def test_split_ip_functions_present(self):
        # 双组拆分/拼接纯函数必须存在：拆分按含冒号归 IPv6，拼接时空组省略
        self.assertIn('function splitIpGroups(', self.html, 'settings.html 缺少 splitIpGroups 函数')
        self.assertIn('function joinIpGroups(', self.html, 'settings.html 缺少 joinIpGroups 函数')

    def test_split_editor_rendering_wired(self):
        # 双组编辑器必须接入渲染、样式、绑定与重置四处分支，否则页面交互不生效
        self.assertIn('if(SPLIT_EDITORS[it.key]){', self.html, 'controlHtml 缺少 SPLIT_EDITORS 分支')
        self.assertIn('ROW_EDITORS[it.key] || SPLIT_EDITORS[it.key]', self.html, 'stacked 样式条件未包含 SPLIT_EDITORS')
        self.assertIn('document.querySelectorAll(\'.split-editor[data-key]\')', self.html, 'bindEvents 缺少双组编辑器绑定')
        self.assertIn('classList.contains(\'split-editor\')', self.html, '重置分支缺少 split-editor 处理')


class NavLinkTests(unittest.TestCase):
    def test_all_pages_link_to_settings(self):
        for name in ("stats.html", "logs.html", "key_pool.html", "requests.html"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('<a href="/settings">配置</a>', text, name)


if __name__ == "__main__":
    unittest.main()
