/** Static navigation text only: searching does not inspect or load settings pages. */
export const SETTINGS_GROUPS = ['Workspace', 'Models & tools', 'Connections', 'App'] as const;
interface Topic { label: string; keywords: string; }
export interface SettingsSection { id: string; group: typeof SETTINGS_GROUPS[number]; label: string; keywords: string; topics: readonly Topic[]; }
export const SETTINGS_SECTIONS: readonly SettingsSection[] = [
  {id:'general',group:'Workspace',label:'General',keywords:'conversation web research generated token model round tool invocation',topics:[
    {label:'Appearance',keywords:'appearance theme accent motion workspace name'},
    {label:'Context & reasoning',keywords:'context window tokens reasoning'},
    {label:'Response defaults',keywords:'response length temperature'},
    {label:'Goal limits',keywords:'goal limits time minutes generated token model round tool invocation'},
    {label:'Startup',keywords:'startup launch sign-in'},
  ]},
  {id:'dictation',group:'Workspace',label:'Dictation',keywords:'voice language',topics:[{label:'Microphone & speech',keywords:'microphone speech transcribe voice language'}]},
  {id:'shortcuts',group:'Workspace',label:'Shortcuts',keywords:'commands',topics:[{label:'Keyboard & HUD',keywords:'keyboard hotkeys HUD commands'}]},
  {id:'models',group:'Models & tools',label:'Models & engines',keywords:'Ollama llama.cpp LM Studio Textgen text-generation-webui Docker Model Runner Jan Desktop LocalAI vLLM SGLang KoboldCpp local',topics:[
    {label:'Engine connections',keywords:'endpoint API key local engine Ollama llama.cpp LM Studio Textgen Docker Model Runner Jan Desktop LocalAI vLLM SGLang KoboldCpp'},
    {label:'Installed models',keywords:'installed selected use model'},
    {label:'Model downloads',keywords:'download Hugging Face Ollama'},
  ]},
  {id:'performance',group:'Models & tools',label:'Performance',keywords:'recommendations speed',topics:[
    {label:'Selected-model tuning',keywords:'hardware CPU GPU local tuning memory'},
    {label:'Measurements & profiles',keywords:'benchmark calibration measured adaptive profiles speed'},
    {label:'Engine controls',keywords:'runtime threads residency warm release memory'},
  ]},
  {id:'openrouter',group:'Models & tools',label:'OpenRouter agents',keywords:'cloud',topics:[{label:'Cloud helpers & consent',keywords:'cloud helpers delegation free API key consent'}]},
  {id:'permissions',group:'Models & tools',label:'Permissions',keywords:'safety',topics:[
    {label:'Permission profiles',keywords:'approval always ask full access deny'},
    {label:'Tool scopes & overrides',keywords:'scope override file edits project computer browser tools'},
  ]},
  {id:'mcp',group:'Connections',label:'MCP connections',keywords:'external tools integration',topics:[{label:'Server connections',keywords:'server endpoint HTTP stdio authentication access token external tools integration'}]},
  {id:'browser',group:'Connections',label:'Browser',keywords:'connect tab client',topics:[
    {label:'Built-in browser',keywords:'native Windows built-in browser WebView2'},
    {label:'Browser access & pairing',keywords:'Chrome Edge extension pairing connect tab client'},
  ]},
  {id:'github',group:'Connections',label:'GitHub',keywords:'',topics:[
    {label:'Account connection',keywords:'token account authentication'},
    {label:'Repositories',keywords:'repositories repository pull requests clone'},
  ]},
  {id:'connections',group:'Connections',label:'Connected workflows',keywords:'external',topics:[{label:'Messaging connections',keywords:'Telegram channel bot external messaging pair'}]},
  {id:'updates',group:'App',label:'Updates & diagnostics',keywords:'',topics:[
    {label:'Version & diagnostics',keywords:'version release crash support logs diagnostics'},
    {label:'Installation & recovery',keywords:'install automatic restart rollback recovery update'},
  ]},
  {id:'storage',group:'App',label:'Storage',keywords:'local disk',topics:[{label:'Local data & backups',keywords:'files folder history backups recovery data local disk worktrees artifacts'}]},
];

export function findSettingsSections(search: string) {
  const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return SETTINGS_SECTIONS.flatMap(section => {
    const text = `${section.label} ${section.keywords} ${section.topics.map(topic => `${topic.label} ${topic.keywords}`).join(' ')}`.toLowerCase();
    if (!words.every(word => text.includes(word))) return [];
    const topicWords = words.filter(word => !section.label.toLowerCase().includes(word));
    const topics = topicWords.length ? section.topics.filter(topic => topicWords.every(word => `${topic.label} ${topic.keywords}`.toLowerCase().includes(word))).map(topic => topic.label) : [];
    return [{section, topics}];
  });
}
