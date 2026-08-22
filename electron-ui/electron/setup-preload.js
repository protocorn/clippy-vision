const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('setup', {




    onStepUpdate: (callback) => {
        ipcRenderer.on('step-update', (_event, data) => callback(data))
    },


    onStepProgress: (callback) => {
        ipcRenderer.on('step-progress', (_event, data) => callback(data))
    },


    onLog: (callback) => {
        ipcRenderer.on('setup-log', (_event, data) => callback(data))
    },


    onOverall: (callback) => {
        ipcRenderer.on('setup-overall', (_event, data) => callback(data))
    },


    onComplete: (callback) => {
        ipcRenderer.on('setup-complete', () => callback())
    },



    getHardwareCheck: () => ipcRenderer.invoke('get-hardware-check'),
    getLLMConfig: () => ipcRenderer.invoke('get-llm-config'),
    saveLLMConfig: (config) => ipcRenderer.invoke('save-llm-config', config || {}),
    openProviderAuth: (provider) => ipcRenderer.invoke('open-provider-auth', provider),


    confirmHardwareAndStart: (opts) => ipcRenderer.invoke('confirm-hardware-and-start', opts || {}),


    retryStep: (key) => ipcRenderer.invoke('retry-step', key),

    getSetupContext: () => ipcRenderer.invoke('get-setup-context'),

    startRecoverySetup: () => ipcRenderer.invoke('start-recovery-setup'),


    launch: () => ipcRenderer.invoke('launch-app'),

})
