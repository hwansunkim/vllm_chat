import { loadModelStatus, initServerEvents } from './servers.js';
import { loadConversations, initConversationEvents } from './conversations.js';
import { initChatEvents } from './chat.js';
import { loadAgents, initAgentEvents } from './agents.js';
import { initMemoryEvents } from './memories.js';
import { initMentionEvents } from './mention.js';
import { initSimulationEvents } from './simulation.js';

loadModelStatus();
loadConversations();
loadAgents();

initServerEvents();
initConversationEvents();
initChatEvents();
initAgentEvents();
initMemoryEvents();
initMentionEvents();
initSimulationEvents();
