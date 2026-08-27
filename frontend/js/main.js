import { loadModelStatus, initServerEvents } from './servers.js';
import { loadConversations, initConversationEvents } from './conversations.js';
import { initChatEvents } from './chat.js';
import { loadAgents, initAgentEvents } from './agents.js';
import { initMemoryEvents } from './memories.js';
import { initMentionEvents } from './mention.js';
import { initSimulationEvents } from './sim/index.js';
import { initSidebarEvents } from './sidebar.js';

loadModelStatus();
loadConversations();
loadAgents();

initSidebarEvents();
initServerEvents();
initConversationEvents();
initChatEvents();
initAgentEvents();
initMemoryEvents();
initMentionEvents();
initSimulationEvents();
