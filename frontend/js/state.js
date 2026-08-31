export const state = {
  currentConvId: null,
  isSending: false,
  // 사고 수준: 'off' | 'low' | 'medium' | 'high'
  //  - thinkingLevel            : 채팅 🧠 컨트롤의 현재 값 (다음 메시지에 그대로 전송)
  //  - currentServerThinkingLevel: 선택된 서버의 기본값 (서버 전환 시 위 값을 여기로 리셋)
  thinkingLevel: 'off',
  currentServerThinkingLevel: 'off',
  webSearchEnabled: false,
  agentList: [],
};
