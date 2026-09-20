export const nameView = document.getElementById('name-view')
export const appShell = document.getElementById('app-shell')
// ARCHIVED: chatView / composer / drawer — see archive/in_app_chat/
export const insightsView = document.getElementById('insights-view')
export const settingsView = document.getElementById('settings-view')
export const timelineView = document.getElementById('timeline-view')
export const nameInput = document.getElementById('name-input')
export const nameSubmit = document.getElementById('name-submit')
export const nameError = document.getElementById('name-error')

export const appBrand = document.getElementById('app-brand')
export const settingsBtn = document.getElementById('settings-btn')
export const navMore = document.getElementById('nav-more')
export const navMoreBtn = document.getElementById('nav-more-btn')
export const navMoreMenu = document.getElementById('nav-more-menu')
export const captureBtn = document.getElementById('capture-btn')
export const captureLabel = document.getElementById('capture-label')

export const insightsDateLabel = document.getElementById('insights-date-label')
export const insightsPrevDay = document.getElementById('insights-prev-day')
export const insightsNextDay = document.getElementById('insights-next-day')
export const insightsRefreshBtn = document.getElementById('insights-refresh-btn')
export const insightsThreadsBtn = document.getElementById('insights-threads-btn')
export const insightsStatus = document.getElementById('insights-status')
export const insightsDayBody = document.getElementById('insights-day-body')
export const insightsThreadsPanel = document.getElementById('insights-threads-panel')
export const insightsThreadsBody = document.getElementById('insights-threads-body')
export const insightsThreadsRefresh = document.getElementById('insights-threads-refresh')

export const timelineBtn = document.getElementById('timeline-btn')
export const timelineBody = document.getElementById('timeline-body')
export const timelineList = document.getElementById('timeline-list')
export const timelineLoadMore = document.getElementById('timeline-load-more')
export const timelineDetailWrap = document.getElementById('timeline-detail-wrap')
export const timelineDetailScroll = document.getElementById('timeline-detail-scroll')
export const timelineDetailContent = document.getElementById('timeline-detail-content')
export const timelineDetailBack = document.getElementById('timeline-detail-back')

export const loadingView = document.getElementById('loading-view')
export const loadingSub = document.getElementById('loading-sub')
export const loadingError = document.getElementById('loading-error')

export const settingsName = document.getElementById('settings-name')
export const settingsIntro = document.getElementById('settings-intro')
export const identityFields = document.getElementById('identity-fields')
export const identityNewKey = document.getElementById('identity-new-key')
export const identityNewVal = document.getElementById('identity-new-val')
export const identityAddBtn = document.getElementById('identity-add-btn')
export const profileStatus = document.getElementById('profile-status')
export const updateCheckToggle = document.getElementById('update-check-toggle')
export const updatesStatus = document.getElementById('updates-status')
export const aboutVersion = document.getElementById('about-version')
export const aboutPlatform = document.getElementById('about-platform')
export const aboutModel = document.getElementById('about-model')
export const aboutMemory = document.getElementById('about-memory')
export const aboutBadge = document.getElementById('about-badge')
export const privacyList = document.getElementById('privacy-list')
export const privacyStatus = document.getElementById('privacy-status')
export const privacyCount = document.getElementById('privacy-count')
export const workspaceRootsList = document.getElementById('workspace-roots-list')
export const workspaceRootInput = document.getElementById('workspace-root-input')
export const workspaceRootAddBtn = document.getElementById('workspace-root-add-btn')
export const workspaceRootsStatus = document.getElementById('workspace-roots-status')
export const settingsUserLabel = document.getElementById('settings-user-label')
export const settingsRuntimeLabel = document.getElementById('settings-runtime-label')
export const settingsRuntimeDot = document.querySelector('.settings-runtime-dot')
export const settingsNavItems = () => Array.from(document.querySelectorAll('.settings-nav-item'))
export const settingsPanels = () => Array.from(document.querySelectorAll('.settings-panel'))
export const mcpReadyLabel = document.getElementById('mcp-ready-label')
export const mcpReadyDetail = document.getElementById('mcp-ready-detail')
export const mcpConfigPreview = document.getElementById('mcp-config-preview')
export const mcpClientList = document.getElementById('mcp-client-list')
export const mcpCopyBtn = document.getElementById('mcp-copy-btn')
export const mcpStatus = document.getElementById('mcp-status')

export const updateBanner = document.getElementById('update-banner')
export const updateBannerText = document.getElementById('update-banner-text')
export const updateBannerLink = document.getElementById('update-banner-link')
export const updateBannerDismiss = document.getElementById('update-banner-dismiss')

export const wideLayoutMq = window.matchMedia('(min-width: 800px)')

export const APP_PANELS = [insightsView, settingsView, timelineView]

export const store = {
 searchTimer: null,
 searchSeq: 0,
 activeSearchQuery: '',
 isLoading: false,
 pendingReply: null,
 identityDraft: {},
 identityCleared: new Set(),
 conversationId: null,
 dismissedUpdateVersion: null,
 timelineSessions: [],
 timelineTotal: 0,
 timelineOffset: 0,
 timelineLoading: false,
 timelineSelectedId: null,
 insightDay: null,
 insightsBusy: false,
 insightsThreadsOpen: false,
}

export const EVENT_TYPE_LABELS = {
 context_change: 'App switch',
 screenshot_analysis: 'Screen capture',
 paste: 'Paste',
 clipboard_change: 'Clipboard',
 typing_burst: 'Typing',
}

export const TIMELINE_PAGE_SIZE = 40

export const MCP_CLIENT_ICONS = {
 cursor: '../assets/cursor_icon.png',
 vscode: '../assets/vscode_icon.png',
 'claude-desktop': '../assets/claude_icon.png',
}

export const PRIVACY_TARGET_ICONS = {
 incognito: '../assets/private_window.png',
 whatsapp: '../assets/whatsapp_icon.png',
 instagram: '../assets/instagram_icon.png',
 telegram: '../assets/telegram_icon.png',
 signal: '../assets/signal.png',
 discord: '../assets/discord_icon.png',
 slack: '../assets/slack_icon.png',
 messages: '../assets/message_icon.png',
}
