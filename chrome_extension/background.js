/**
 * Background service worker — tab audio capture and side panel management.
 *
 * Responsibilities:
 *   1. Provide tab capture stream IDs to the side panel
 *   2. Enable the side panel on YouTube watch pages
 *   3. Relay messages between content script and side panel
 */

// Enable side panel on YouTube watch pages
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!tab.url) return;
  const isYouTube = tab.url.includes("youtube.com/watch");
  chrome.sidePanel.setOptions({
    tabId,
    enabled: isYouTube,
    path: isYouTube ? "sidepanel.html" : undefined,
  });
});

// Open side panel when extension icon is clicked
chrome.action.onClicked.addListener((tab) => {
  chrome.sidePanel.open({ tabId: tab.id });
});

// Handle messages from side panel and content scripts
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "capture-tab-audio") {
    handleTabCapture(msg.tabId, sendResponse);
    return true; // async response
  }

  if (msg.type === "get-active-tab") {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      sendResponse(tabs[0] || null);
    });
    return true;
  }

  // Relay content script messages to side panel (and vice versa)
  if (msg.type === "yt-state-update" || msg.type === "yt-video-info") {
    // Forward from content script to side panel — side panel listens via
    // its own onMessage handler
    // No explicit forwarding needed; side panel calls chrome.tabs.sendMessage
    // to reach the content script. Content script messages arrive here and
    // we can store/forward them.
    //
    // For simplicity, store latest state so side panel can poll.
    if (msg.type === "yt-video-info") {
      latestVideoInfo[sender.tab?.id] = msg;
    }
    if (msg.type === "yt-state-update") {
      latestVideoState[sender.tab?.id] = msg;
    }
  }

  if (msg.type === "get-video-info") {
    const info = latestVideoInfo[msg.tabId] || null;
    sendResponse(info);
    return false;
  }

  if (msg.type === "get-video-state") {
    const state = latestVideoState[msg.tabId] || null;
    sendResponse(state);
    return false;
  }
});

// In-memory cache for video info from content scripts
const latestVideoInfo = {};
const latestVideoState = {};

/**
 * Capture tab audio and return a stream ID to the caller.
 *
 * The stream ID is passed to the side panel which calls getUserMedia()
 * with chromeMediaSource: "tab" to get a MediaStream of the tab's audio.
 */
function handleTabCapture(tabId, sendResponse) {
  const targetTabId = tabId || undefined;

  // If a specific tab ID was given, use it. Otherwise capture the active tab.
  if (targetTabId) {
    captureTab(targetTabId, sendResponse);
  } else {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (!tabs[0]) {
        sendResponse({ error: "No active tab" });
        return;
      }
      captureTab(tabs[0].id, sendResponse);
    });
  }
}

function captureTab(tabId, sendResponse) {
  chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (streamId) => {
    if (chrome.runtime.lastError) {
      console.error("[bg] Tab capture error:", chrome.runtime.lastError.message);
      sendResponse({ error: chrome.runtime.lastError.message });
      return;
    }
    console.log("[bg] Tab capture stream ID obtained for tab", tabId);
    sendResponse({ streamId });
  });
}
