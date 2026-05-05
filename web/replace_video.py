with open('index.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the video-view block (lines 215-243, 0-indexed: 215-243)
# Line 215 (0-indexed 215) is blank before video-view
# Line 216 (0-indexed 216) starts video-view
# Line 244 (0-indexed 244) ends video-view

start_idx = 215  # 0-indexed, the blank line before
end_idx = 244    # 0-indexed, the closing </div>

new_block = '''\n    <div class="video-view hidden" id="videoView">
      <aside class="video-sidebar" id="videoSidebar">
        <div class="sidebar-section">
          <h3 class="sidebar-heading">档位</h3>
          <div class="filter-group" id="videoTierSidebarFilters">
            <label class="filter-item"><input type="radio" name="video_tier" value="" checked><span class="filter-label">全部</span></label>
            <label class="filter-item"><input type="radio" name="video_tier" value="normal"><span class="filter-label">正常</span></label>
            <label class="filter-item"><input type="radio" name="video_tier" value="light_spicy"><span class="filter-label">轻荤</span></label>
            <label class="filter-item"><input type="radio" name="video_tier" value="heavy_spicy"><span class="filter-label">重荤</span></label>
          </div>
        </div>
        <div class="sidebar-section">
          <h3 class="sidebar-heading">人格</h3>
          <div class="filter-group" id="videoPersonaSidebarFilters">
            <label class="filter-item"><input type="radio" name="video_persona" value="" checked><span class="filter-label">全部</span></label>
          </div>
        </div>
        <div class="sidebar-section">
          <button class="btn btn-accent btn-block" id="videoGotoImagesBtn" style="width:100%;">返回图片库</button>
        </div>
      </aside>
      <div class="video-main">
        <div class="video-toolbar">
          <div class="video-toolbar-right">
            <button id="videoSettingsBtn" class="btn btn-secondary btn-sm" title="视频设置">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l-.06.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              设置
            </button>
          </div>
        </div>
        <div class="video-grid" id="videoGrid"></div>
        <div class="empty-state hidden" id="videoEmptyState">
          <span class="empty-icon">🎬</span>
          <p class="empty-text">还没有视频</p>
          <p class="empty-hint">去图片库选一张图片转视频吧</p>
        </div>
        <div class="video-pagination" id="videoPagination"></div>
      </div>
    </div>\n'''

new_lines = lines[:start_idx] + [new_block] + lines[end_idx+1:]

with open('index.html', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print('Done')
