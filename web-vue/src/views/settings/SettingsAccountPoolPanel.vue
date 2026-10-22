<template>
  <div class="space-y-4">
    <FormSection title="GPT 号池调度">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField v-for="field in schedulingFields" :key="field.key" :label="field.label">
          <template #label-extra>
            <HelpTip :text="field.help" />
          </template>
          <SettingsNumberInput :field="field.control" />
        </FormField>
      </div>
    </FormSection>

    <FormSection title="文字对话">
      <div class="settings-check-grid settings-check-grid--single">
        <div v-for="toggle in chatToggles" :key="toggle.key" class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings[toggle.key]">{{ toggle.label }}</Checkbox>
            <HelpTip :text="toggle.help" />
          </div>
        </div>
      </div>
    </FormSection>

    <FormSection title="生图加速">
      <div class="settings-check-grid settings-check-grid--single">
        <div v-for="toggle in imageToggles" :key="toggle.key" class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings[toggle.key]">{{ toggle.label }}</Checkbox>
            <HelpTip :text="toggle.help" />
          </div>
        </div>
      </div>
      <FormField label="小图放大引擎">
        <template #label-extra>
          <HelpTip text="仅在开启“小图自动放大”后生效。默认 Pillow（无额外依赖）；选 sharp 需要镜像或主机装有 Node 与 sharp，不可用时自动回退 Pillow 并记录日志。" />
        </template>
        <div class="w-full">
          <GroupedSelectMenu
            v-model="upscaleEngine"
            :options="imageUpscaleEngineOptions"
            selected-indicator="none"
            aria-label="小图放大引擎"
            :disabled="!settings.image_upscale_enabled"
            block
          />
        </div>
      </FormField>
      <FormField label="生图指令后续读等待（秒）">
        <template #label-extra>
          <HelpTip text="上游下发生图指令后继续读同一条流等待图片 ID 的最长时间，超时再转轮询。0 表示收到指令即断开，回到旧行为。范围 0–120。" />
        </template>
        <SettingsNumberInput :field="postToolWaitField" />
      </FormField>
    </FormSection>

    <FormSection title="凭据失效自动重新登录">
      <div class="settings-check-grid settings-check-grid--single">
        <div class="settings-check-item">
          <div class="settings-check-control">
            <Checkbox v-model="settings.account_relogin_enabled">启用自动重新登录</Checkbox>
            <HelpTip text="默认关闭。refresh_token 被明确拒绝的账号，若保存了 OutlookToken 邮箱凭据（或邮箱仍在 OutlookToken 邮箱池中），后台会用邮箱验证码免密码登录换取新凭据；最多 3 次，间隔 30 分钟、2 小时。开启后“自动移除异常账号”会先保留这些账号。" />
          </div>
        </div>
      </div>
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <FormField label="扫描间隔（秒）">
          <template #label-extra>
            <HelpTip text="后台检查待重新登录账号的间隔，范围 60–3600。" />
          </template>
          <SettingsNumberInput :field="reloginIntervalField" :disabled="!settings.account_relogin_enabled" />
        </FormField>
        <FormField label="同时登录数">
          <template #label-extra>
            <HelpTip text="同时进行的重新登录数量，范围 1–4；发码仍受注册的全局发码限速约束。" />
          </template>
          <SettingsNumberInput :field="reloginConcurrencyField" :disabled="!settings.account_relogin_enabled" />
        </FormField>
      </div>
    </FormSection>
  </div>
</template>

<script setup lang="ts">
import SettingsNumberInput from './SettingsNumberInput.vue'
import { Checkbox, FormField, FormSection, HelpTip, Input } from 'nanocat-ui'
import { computed } from 'vue'
import GroupedSelectMenu from '@/components/ui/GroupedSelectMenu.vue'
import type { Settings } from '@/types/api'
import { imageUpscaleEngineOptions } from '@/views/settings/settingsView'
import { useNumberSettingField } from '@/views/settings/useNumberSettingField'

type NumberKey =
  | 'image_account_cooldown_secs'
  | 'image_preflight_fresh_secs'
  | 'image_max_account_attempts'
  | 'image_stream_post_tool_wait_secs'
  | 'image_queue_limit'
  | 'account_processing_concurrency'
  | 'account_auto_pause_strikes'
  | 'account_auto_pause_hours'
  | 'new_account_cooldown_hours'
  | 'new_account_ramp_days'
  | 'new_account_ramp_daily_images'
  | 'text_account_concurrency'
  | 'account_failure_cooldown_secs'
  | 'account_relogin_interval_secs'
  | 'account_relogin_concurrency'

type ToggleKey =
  | 'image_account_retry_enabled'
  | 'image_poll_adaptive'
  | 'image_warm_enabled'
  | 'image_stream_direct_resolve'
  | 'image_drop_extra_variants'
  | 'image_upscale_enabled'
  | 'chat_sentinel_vm_enabled'
  | 'chat_temporary_mode'

const props = defineProps<{
  settings: Settings
}>()

const upscaleEngine = computed({
  get: () => props.settings.image_upscale_engine || 'pillow_lanczos',
  set: (value: string) => {
    props.settings.image_upscale_engine = value === 'sharp_lanczos3' ? 'sharp_lanczos3' : 'pillow_lanczos'
  },
})

function numberField(key: NumberKey, fallback: number, min: number, max: number, enabled?: () => boolean) {
  return useNumberSettingField(
    () => props.settings[key] ?? fallback,
    (value) => { props.settings[key] = value },
    { integer: true, min, max, fallback, enabled },
  )
}

const schedulingFields = [
  {
    key: 'image_account_cooldown_secs',
    label: '单账号生图冷却（秒）',
    help: '一个账号完成生图后需休息多久才接新请求。0 关闭冷却（默认），范围 0–600。号少时开启会增加排队时间。',
    fallback: 0,
    control: numberField('image_account_cooldown_secs', 0, 0, 600),
  },
  {
    key: 'image_preflight_fresh_secs',
    label: '预检免检窗口（秒）',
    help: '账号在此时间内已远程确认正常且之后没有生图失败时，选号跳过远程预检。0 表示每次都预检，范围 0–3600。',
    fallback: 600,
    control: numberField('image_preflight_fresh_secs', 600, 0, 3600),
  },
  {
    key: 'image_max_account_attempts',
    label: '单图最多尝试账号数',
    help: '额度耗尽、429、上游报错回复等可换号的失败，一张图最多依次尝试几个账号（含首次），默认 4，范围 1–10。需先开启下方“生图失败自动换号”；设为 1 等同不换号。',
    fallback: 4,
    control: numberField('image_max_account_attempts', 4, 1, 10),
  },
  {
    key: 'image_queue_limit',
    label: '图片任务排队上限',
    help: '后台图片任务同时在途（排队 + 运行）的最大数量，超过后新任务直接返回 503（image_task_queue_full）。0 表示自动，只受内置队列容量限制；范围 0–10000。',
    fallback: 0,
    control: numberField('image_queue_limit', 0, 0, 10000),
  },
  {
    key: 'account_processing_concurrency',
    label: '账号批处理并发',
    help: '批量刷新账号、导入 CPA 凭据等批处理同时进行的最大请求数，范围 1–100，默认 30。调低可减轻代理和上游压力。',
    fallback: 30,
    control: numberField('account_processing_concurrency', 30, 1, 100),
  },
  {
    key: 'account_auto_pause_strikes',
    label: '自动暂停阈值',
    help: '长时保护：连续失败 3 次记一次失败记录，6 小时内累计达到该次数即自动暂停（仅排到最后，其它号都不可用时仍会使用）。0 关闭，范围 0–20。每一轮连续失败还会先触发下方“失败冷却”的短时冷却。',
    fallback: 3,
    control: numberField('account_auto_pause_strikes', 3, 0, 20),
  },
  {
    key: 'account_auto_pause_hours',
    label: '自动暂停时长（小时）',
    help: '自动暂停持续时间，账号成功一次即提前解除。范围 1–72。',
    fallback: 6,
    control: numberField('account_auto_pause_hours', 6, 1, 72),
  },
  {
    key: 'account_failure_cooldown_secs',
    label: '失败冷却（秒）',
    help: '短时保护：账号连续失败 3 次后立即冷却这么久，期间只在没有其它号时才使用；成功一次即解除。冷却只看连续失败，与上面按累计次数触发的长时“自动暂停”互补。0 关闭，范围 0–3600，默认 300。',
    fallback: 300,
    control: numberField('account_failure_cooldown_secs', 300, 0, 3600),
  },
  {
    key: 'text_account_concurrency',
    label: '对话账号并发',
    help: '单个账号同时承载的对话 / 搜索请求数，账号都满时新请求排队等待（最长 2 分钟）。范围 1–20，默认 3。与生图的“账号并发”相互独立。',
    fallback: 3,
    control: numberField('text_account_concurrency', 3, 1, 20),
  },
  {
    key: 'new_account_cooldown_hours',
    label: '新号冷静期（小时）',
    help: '仅对注册流程新入池的号生效：入池后这段时间内不接生图也不接对话；手动导入的老号不受影响。0 关闭，范围 0–168，默认 12。',
    fallback: 12,
    control: numberField('new_account_cooldown_hours', 12, 0, 168),
  },
  {
    key: 'new_account_ramp_days',
    label: '新号放量期（天）',
    help: '冷静期结束后的放量期长度；期内每个账号每天（北京时间）生图张数受下方上限约束，对话不受限。0 关闭，范围 0–30，默认 3。',
    fallback: 3,
    control: numberField('new_account_ramp_days', 3, 0, 30),
  },
  {
    key: 'new_account_ramp_daily_images',
    label: '放量期每日生图上限',
    help: '放量期内单个新号每天最多生图张数，用完后到次日 0 点（北京时间）恢复。范围 1–200，默认 5。',
    fallback: 5,
    control: numberField('new_account_ramp_daily_images', 5, 1, 200),
  },
] as const

const postToolWaitField = numberField('image_stream_post_tool_wait_secs', 45, 0, 120)
const reloginIntervalField = numberField('account_relogin_interval_secs', 300, 60, 3600, () => Boolean(props.settings.account_relogin_enabled))
const reloginConcurrencyField = numberField('account_relogin_concurrency', 1, 1, 4, () => Boolean(props.settings.account_relogin_enabled))

const chatToggles: Array<{ key: ToggleKey, label: string, help: string }> = [
  {
    key: 'chat_temporary_mode',
    label: '对话使用临时对话（不留历史）',
    help: '默认开启，对话不会出现在账号历史里。只影响文字对话；上游不支持在临时对话中生图。',
  },
]

const imageToggles: Array<{ key: ToggleKey, label: string, help: string }> = [
  {
    key: 'image_account_retry_enabled',
    label: '生图失败自动换号',
    help: '默认开启。可换号的失败会换一个账号重试，最多尝试“单图最多尝试账号数”个账号；关闭后失败即返回。',
  },
  {
    key: 'image_stream_direct_resolve',
    label: '流内拿到图片 ID 后直接解析',
    help: '默认开启。流里已拿到图片 ID 时直接解析下载地址，不再做二次确认；解析不到才回退轮询。开启后“图片二次确认机制”在这种情况下不生效。',
  },
  {
    key: 'image_poll_adaptive',
    label: '动态轮询间隔',
    help: '默认开启。以“图片轮询间隔”为基准，按账号和出口动态调整（1–16 秒），遇 429 翻倍、正常时逐步缩短。',
  },
  {
    key: 'image_warm_enabled',
    label: '空闲账号预热',
    help: '默认开启。近 3 分钟有生图时，为最多 6 个空闲账号预先加载首页并取好令牌，有效 120 秒、只用一次，缩短出图时间。',
  },
  {
    key: 'image_drop_extra_variants',
    label: '二选一时只保留第一张',
    help: '默认开启。上游并行生成两个候选时每个会话只取第一张，避免返回张数超过请求的 n。',
  },
  {
    key: 'image_upscale_enabled',
    label: '小图自动放大',
    help: '默认关闭。上游返回的图片小于请求尺寸时，在本地用 Lanczos 放大到目标尺寸，引擎见下方“小图放大引擎”。',
  },
  {
    key: 'chat_sentinel_vm_enabled',
    label: '对话链路使用官方 Sentinel SDK（实验）',
    help: '默认关闭。由 Node VM 运行官方 Sentinel SDK 生成对话 / 生图的 chat-requirements，需要 Node.js；上游拒绝时自动退回 Python PoW。',
  },
]
</script>

<style scoped>
.settings-check-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13.5rem, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.settings-check-grid--single {
  grid-template-columns: minmax(0, 1fr);
}

.settings-check-item {
  min-height: 38px;
  border: 1px solid hsl(var(--border));
  border-radius: 14px;
  background: hsl(var(--background) / 0.72);
  transition:
    border-color 0.16s ease,
    background-color 0.16s ease;
}

.settings-check-item:hover {
  border-color: hsl(var(--foreground) / 0.18);
  background: hsl(var(--muted) / 0.24);
}

.settings-check-control {
  display: flex;
  min-height: 38px;
  align-items: center;
  gap: 8px;
  padding-right: 10px;
}

.settings-check-item :deep(label) {
  display: flex;
  width: 100%;
  flex: 1;
  min-height: 38px;
  align-items: center;
  gap: 10px;
  padding: 9px 11px;
}

.settings-check-item :deep(label > span:last-child) {
  color: hsl(var(--foreground) / 0.78);
  line-height: 1.35;
}
</style>
