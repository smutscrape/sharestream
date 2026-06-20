document.addEventListener('DOMContentLoaded', () => {
    let backendApiBase = '';
    if (window.location.pathname.includes('/static/admin.html')) {
        backendApiBase = window.location.origin; 
    } else {
        backendApiBase = window.SHARESTREAM_PROXY_API_BASE || '';
    }

    const loginSection = document.getElementById('login-section');
    const adminContent = document.getElementById('admin-content');
    const loginForm = document.getElementById('login-form');
    const loginError = document.getElementById('login-error');
    const logoutButton = document.getElementById('logout-button');

    const shareForm = document.getElementById('share-form');
    const stashIdInput = document.getElementById('stash-id');
    const videoNameInput = document.getElementById('video-name');
    const daysValidInput = document.getElementById('days-valid');
    const resolutionInput = document.getElementById('resolution');
    const sharePasswordInput = document.getElementById('share-password');
    const showInGalleryInput = document.getElementById('show-in-gallery');
    const lookupTitleButton = document.getElementById('lookup-title-button');
    const shareMessage = document.getElementById('share-message');
    const shareError = document.getElementById('share-error');
    const videoShareIdTypeSelect = document.getElementById('share-id-type-video');
    const videoCustomShareIdInput = document.getElementById('custom-share-id-video');
    const videoEmbedModeSelect = document.getElementById('embed-mode-video');
    const tagEmbedModeSelect = document.getElementById('embed-mode-tag');

    const shareTagForm = document.getElementById('share-tag-form');
    const tagNameInput = document.getElementById('tag-name');
    const tagIdInput = document.getElementById('tag-id');
    const shareIdTypeSelect = document.getElementById('share-id-type');
    const customShareIdInput = document.getElementById('custom-share-id');
    const tagDaysValidInput = document.getElementById('tag-days-valid');
    const tagResolutionInput = document.getElementById('tag-resolution');
    const tagSharePasswordInput = document.getElementById('tag-share-password');
    const tagShowInGalleryInput = document.getElementById('tag-show-in-gallery');
    const tagApplyLimitTagInput = document.getElementById('tag-apply-limit-tag');
    const tagApplyLimitWrapper = document.getElementById('tag-apply-limit-wrapper');
    const tagGalleryModeInput = document.getElementById('tag-gallery-mode');
    const tagDefaultSortSelect = document.getElementById('default-sort-tag');
    const lookupTagButton = document.getElementById('lookup-tag-button');
    const tagShareMessage = document.getElementById('tag-share-message');
    const tagShareError = document.getElementById('tag-share-error');

    const sharedVideosTableBody = document.querySelector('#shared-videos-table tbody');
    const sharedTagsTableBody = document.querySelector('#shared-tags-table tbody');
    const refreshSharesButton = document.getElementById('refresh-shares');
    const clearCacheButton = document.getElementById('clear-cache');

    const editModal = document.getElementById('edit-modal');
    const editShareIdInput = document.getElementById('edit-share-id');
    const editVideoNameInput = document.getElementById('edit-video-name');
    const editDaysValidInput = document.getElementById('edit-days-valid');
    const editResolutionInput = document.getElementById('edit-resolution');
    const editSharePasswordInput = document.getElementById('edit-share-password');
    const editShowInGalleryInput = document.getElementById('edit-show-in-gallery');
    const editEmbedModeSelect = document.getElementById('edit-embed-mode');
    const editClearPasswordInput = document.getElementById('edit-clear-password');
    const saveEditButton = document.getElementById('save-edit-button');
    const cancelEditButton = document.getElementById('cancel-edit-button');
    const editError = document.getElementById('edit-error');

    const editTagModal = document.getElementById('edit-tag-modal');
    const editTagShareIdInput = document.getElementById('edit-tag-share-id');
    const editTagNameInput = document.getElementById('edit-tag-name');
    const editTagIdInput = document.getElementById('edit-tag-id');
    const editTagDaysValidInput = document.getElementById('edit-tag-days-valid');
    const editTagResolutionInput = document.getElementById('edit-tag-resolution');
    const editTagEmbedModeSelect = document.getElementById('edit-tag-embed-mode');
    const editTagDefaultSortSelect = document.getElementById('edit-tag-default-sort');
    const editTagSharePasswordInput = document.getElementById('edit-tag-share-password');
    const editTagShowInGalleryInput = document.getElementById('edit-tag-show-in-gallery');
    const editTagApplyLimitTagInput = document.getElementById('edit-tag-apply-limit-tag');
    const editTagApplyLimitWrapper = document.getElementById('edit-tag-apply-limit-wrapper');
    const editTagGalleryModeInput = document.getElementById('edit-tag-gallery-mode');
    const editTagClearPasswordInput = document.getElementById('edit-tag-clear-password');
    const saveEditTagButton = document.getElementById('save-edit-tag-button');
    const cancelEditTagButton = document.getElementById('cancel-edit-tag-button');
    const editTagError = document.getElementById('edit-tag-error');

    let authToken = localStorage.getItem('share_token');

    // Store passwords for shares created in this session
    const sharePasswords = {};

    function showLogin() {
        loginSection.style.display = 'flex'; 
        adminContent.style.display = 'none';
        localStorage.removeItem('share_token');
        authToken = null;
    }

    function showAdmin() {
        loginSection.style.display = 'none';
        adminContent.style.display = 'flex'; 
        loginError.textContent = '';
        fetchSharedContent();
        setBaseDomain();
    }

    function clearMessages() {
        loginError.textContent = '';
        shareMessage.textContent = '';
        shareError.textContent = '';
        tagShareMessage.textContent = '';
        tagShareError.textContent = '';
        editError.textContent = '';
    }

    function logDebug(message, data = null) {
        console.log(`[DEBUG] ${new Date().toISOString()} ${message}`, data || '');
        if (data) {
            try {
                console.table(data);
            } catch (e) {
                // console.table might fail
            }
        }
    }

    async function apiRequest(url, method = 'GET', body = null, requiresAuth = true) {
        const fullUrl = backendApiBase + url;
        logDebug(`API Request: ${method} ${fullUrl}`, body);
        
        const headers = {};
        if (method !== 'POST' || url !== '/login') {
            headers['Content-Type'] = 'application/json';
        }

        if (requiresAuth) {
            if (!authToken) {
                showLogin();
                throw new Error('Not authenticated');
            }
            headers['Authorization'] = `Bearer ${authToken}`;
        }

        const options = {
            method,
            headers,
        };

        if (body) {
            if (method === 'POST' && url === '/login') {
                options.body = body; 
            } else {
                options.body = JSON.stringify(body);
            }
        }

        try {
            const response = await fetch(fullUrl, options);
            logDebug(`API Response: ${response.status} ${response.statusText} for ${method} ${fullUrl}`);
            
            if (response.status === 401 && requiresAuth) {
                showLogin();
                throw new Error('Authentication failed or token expired.');
            }
            if (!response.ok) {
                let errorData = { detail: `HTTP error! status: ${response.status}`};
                try {
                    errorData = await response.json();
                } catch (e) {
                    errorData.detail = response.statusText || errorData.detail;
                }
                logDebug('API Error Response:', errorData);
                throw new Error(errorData.detail || `HTTP error! status: ${response.status}`);
            }
            if (response.status === 204 || response.headers.get('content-length') === '0') {
                logDebug('API Success Response (No Content)');
                return null;
            }
            const responseData = await response.json();
            logDebug('API Success Response:', responseData);
            return responseData;
        } catch (error) {
            console.error(`API Request Error for ${method} ${fullUrl}:`, error.message);
            throw error; 
        }
    }

    function escapeHTML(str) {
        if (typeof str !== 'string') return '';
        const div = document.createElement('div');
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }

    function calculateDaysRemaining(expiresAt) {
        const now = new Date();
        const expiry = new Date(expiresAt);
        const diffTime = expiry - now;
        const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
        return Math.max(0, diffDays);
    }

    function getRelativeTime(expiresAt) {
        const now = new Date();
        const expiry = new Date(expiresAt);
        const diffMs = expiry - now;
        
        if (diffMs < 0) return 'expired';
        
        const minutes = Math.floor(diffMs / 60000);
        const hours = Math.floor(minutes / 60);
        const days = Math.floor(hours / 24);
        const months = Math.floor(days / 30);
        const years = Math.floor(days / 365);
        
        if (minutes < 60) return `${minutes}m`;
        if (hours < 24) return `${hours}h`;
        if (days < 30) return `${days}d`;
        if (months < 12) return `${months}mo`;
        return `${years}y`;
    }
    
    function truncateText(text, maxLength) {
        if (text.length <= maxLength) return text;
        return text.substring(0, maxLength) + '...';
    }

    function copyToClipboard(text) {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(() => {
                alert('Link copied to clipboard!');
            }).catch(err => {
                console.error('Failed to copy link:', err);
                fallbackCopy(text);
            });
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        try {
            document.execCommand('copy');
            alert('Link copied to clipboard!');
        } catch (err) {
            console.error('Fallback copy failed:', err);
            alert('Failed to copy link. Please copy manually: ' + text);
        }
        document.body.removeChild(textarea);
    }

    if (shareIdTypeSelect) {
        shareIdTypeSelect.addEventListener('change', () => {
            const customShareIdLabel = document.querySelector('label[for="custom-share-id"]');
            if (shareIdTypeSelect.value === 'custom') {
                customShareIdInput.style.display = 'block';
                customShareIdLabel.style.display = 'block';
                customShareIdInput.required = true;
            } else {
                customShareIdInput.style.display = 'none';
                customShareIdLabel.style.display = 'none';
                customShareIdInput.required = false;
                customShareIdInput.value = '';
            }
        });
    }

    if (videoShareIdTypeSelect) {
        videoShareIdTypeSelect.addEventListener('change', () => {
            const videoCustomShareIdLabel = document.querySelector('label[for="custom-share-id-video"]');
            if (videoShareIdTypeSelect.value === 'custom') {
                videoCustomShareIdInput.style.display = 'block';
                videoCustomShareIdLabel.style.display = 'block';
                videoCustomShareIdInput.required = true;
            } else {
                videoCustomShareIdInput.style.display = 'none';
                videoCustomShareIdLabel.style.display = 'none';
                videoCustomShareIdInput.required = false;
                videoCustomShareIdInput.value = '';
            }
        });
    }

    // A home-featured share is ALWAYS limited to limit_to_tag, so the "Apply tag
    // limit?" toggle only matters for non-featured shares. When "Feature on Home?"
    // is on, force the apply-limit toggle checked + disabled to reflect that.
    function syncApplyLimitState() {
        if (!tagApplyLimitTagInput || !tagShowInGalleryInput) return;
        const featured = tagShowInGalleryInput.checked;
        tagApplyLimitTagInput.disabled = featured;
        if (featured) tagApplyLimitTagInput.checked = true;
        if (tagApplyLimitWrapper) tagApplyLimitWrapper.style.opacity = featured ? '0.5' : '1';
    }
    function syncEditApplyLimitState() {
        if (!editTagApplyLimitTagInput || !editTagShowInGalleryInput) return;
        const featured = editTagShowInGalleryInput.checked;
        editTagApplyLimitTagInput.disabled = featured;
        if (featured) editTagApplyLimitTagInput.checked = true;
        if (editTagApplyLimitWrapper) editTagApplyLimitWrapper.style.opacity = featured ? '0.5' : '1';
    }
    if (tagShowInGalleryInput) {
        tagShowInGalleryInput.addEventListener('change', syncApplyLimitState);
        syncApplyLimitState();
    }
    if (editTagShowInGalleryInput) {
        editTagShowInGalleryInput.addEventListener('change', syncEditApplyLimitState);
    }

    if (authToken) {
        showAdmin();
    } else {
        showLogin();
    }

    if (loginForm) {
        loginForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            clearMessages();
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            
            const formData = new URLSearchParams();
            formData.append('username', username);
            formData.append('password', password);

            try {
                const data = await apiRequest('/login', 'POST', formData, false);
                authToken = data.access_token;
                localStorage.setItem('share_token', authToken);
                showAdmin();
            } catch (error) {
                console.error('Login failed:', error);
                loginError.textContent = error.message;
                showLogin();
            }
        });
    }
    
    if (logoutButton) {
        logoutButton.addEventListener('click', () => {
            showLogin();
        });
    }

    if (lookupTitleButton) {
        lookupTitleButton.addEventListener('click', async () => {
            clearMessages();
            const stashId = stashIdInput.value;
            if (!stashId) {
                shareError.textContent = 'Please enter a Stash Video ID.';
                return;
            }
            
            logDebug('Looking up video title for ID:', stashId);
            
            try {
                const data = await apiRequest(`/get_video_title/${stashId}`);
                if (data && data.title) {
                    videoNameInput.value = data.title;
                    logDebug('Video title found:', data.title);
                } else {
                    shareError.textContent = 'Could not find title for this ID.';
                    videoNameInput.value = '';
                    logDebug('No title found for video ID:', stashId);
                }
            } catch (error) {
                shareError.textContent = `Error looking up title: ${error.message}`;
                videoNameInput.value = '';
                logDebug('Error looking up video title:', error);
            }
        });
    }

    if (shareForm) {
        shareForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            clearMessages();

            let videoCustomShareId = null;
            if (videoShareIdTypeSelect && videoShareIdTypeSelect.value === 'custom') {
                videoCustomShareId = (videoCustomShareIdInput.value || '').trim().toLowerCase().replace(/[^a-z0-9-_]/g, '-').replace(/-+/g, '-');
                if (!videoCustomShareId) {
                    shareError.textContent = 'Please enter a custom share ID.';
                    return;
                }
            }

            const shareData = {
                video_name: videoNameInput.value,
                stash_video_id: parseInt(stashIdInput.value, 10),
                days_valid: parseInt(daysValidInput.value, 10),
                resolution: resolutionInput.value,
                password: sharePasswordInput.value || null,
                show_in_gallery: showInGalleryInput.checked,
                custom_share_id: videoCustomShareId,
                embed_mode: (videoEmbedModeSelect && videoEmbedModeSelect.value) || null
            };

            logDebug('Sharing video with data:', shareData);

            if (!shareData.video_name || isNaN(shareData.stash_video_id) || isNaN(shareData.days_valid) || !shareData.resolution) {
                shareError.textContent = 'Please fill in all required fields correctly.';
                return;
            }

            try {
                const result = await apiRequest('/share', 'POST', shareData);
                shareMessage.textContent = `Video shared successfully! URL: ${result.share_url}`;
                shareForm.reset();
                fetchSharedContent();
                logDebug('Video shared successfully:', result);
                // Store the password for this share
                if (shareData.password) {
                    const shareId = result.share_url.split('/').pop().split('?')[0];
                    sharePasswords[shareId] = shareData.password;
                }
            } catch (error) {
                shareError.textContent = `Failed to share video: ${error.message}`;
                logDebug('Failed to share video:', error);
            }
        });
    }
    
    if (lookupTagButton) {
        lookupTagButton.addEventListener('click', async () => {
            clearMessages();
            const tagName = tagNameInput.value.trim();
            if (!tagName) {
                tagShareError.textContent = 'Please enter a tag name.';
                return;
            }
            
            logDebug('Looking up tag:', tagName);
            
            try {
                const data = await apiRequest(`/lookup_tag/${encodeURIComponent(tagName)}`);
                logDebug('Tag lookup response:', data);
                
                if (data && data.tag_info) {
                    tagIdInput.value = data.tag_info.id;
                    tagIdInput.placeholder = `${data.tag_info.name} (${data.video_count} videos)`;
                    tagShareError.textContent = '';
                    logDebug('Tag found:', data.tag_info);
                } else {
                    tagShareError.textContent = 'Tag not found or has no videos.';
                    tagIdInput.value = '';
                    tagIdInput.placeholder = 'Auto-filled after lookup';
                    logDebug('Tag not found:', tagName);
                }
            } catch (error) {
                tagShareError.textContent = `Error looking up tag: ${error.message}`;
                tagIdInput.value = '';
                tagIdInput.placeholder = 'Auto-filled after lookup';
                logDebug('Error looking up tag:', error);
            }
        });
    }

    if (shareTagForm) {
        shareTagForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            clearMessages();

            let customShareId = null;
            if (shareIdTypeSelect.value === 'custom') {
                customShareId = customShareIdInput.value.trim();
                if (!customShareId) {
                    tagShareError.textContent = 'Please enter a custom share ID.';
                    return;
                }
            } else if (shareIdTypeSelect.value === 'tag-name') {
                customShareId = tagNameInput.value.trim().toLowerCase().replace(/[^a-z0-9-_]/g, '-').replace(/-+/g, '-');
                 if (!customShareId) {
                    tagShareError.textContent = 'Cannot derive share ID from empty tag name.';
                    return;
                }
            }

            const shareTagData = {
                tag_name: tagNameInput.value.trim(),
                tag_id: tagIdInput.value.trim(),
                days_valid: parseInt(tagDaysValidInput.value, 10),
                resolution: tagResolutionInput.value,
                password: tagSharePasswordInput.value || null,
                show_in_gallery: tagShowInGalleryInput.checked,
                apply_limit_tag: tagApplyLimitTagInput ? tagApplyLimitTagInput.checked : true,
                gallery_mode: tagGalleryModeInput ? tagGalleryModeInput.checked : false,
                custom_share_id: customShareId,
                embed_mode: (tagEmbedModeSelect && tagEmbedModeSelect.value) || null,
                default_sort: (tagDefaultSortSelect && tagDefaultSortSelect.value) || null
            };

            logDebug('Sharing tag with data:', shareTagData);

            if (!shareTagData.tag_name || !shareTagData.tag_id || isNaN(shareTagData.days_valid) || !shareTagData.resolution) {
                tagShareError.textContent = 'Please fill in all required fields correctly.';
                return;
            }

            try {
                const result = await apiRequest('/share_tag', 'POST', shareTagData);
                tagShareMessage.textContent = `Tag shared successfully! URL: ${result.share_url} (${result.video_count} videos)`;
                // Stash the plaintext password (the server only keeps its hash) so
                // the Copy button can append ?pwd= for the rest of this session.
                if (shareTagData.password && result.share_id) {
                    sharePasswords[result.share_id] = shareTagData.password;
                }
                shareTagForm.reset();
                syncApplyLimitState();  // reset() re-checks defaults; re-sync disabled state
                tagIdInput.value = '';
                tagIdInput.placeholder = 'Auto-filled after lookup';
                if(shareIdTypeSelect) shareIdTypeSelect.value = 'random';
                if(customShareIdInput) customShareIdInput.style.display = 'none';
                const customShareIdLabel = document.querySelector('label[for="custom-share-id"]');
                if(customShareIdLabel) customShareIdLabel.style.display = 'none';
                fetchSharedContent();
                logDebug('Tag shared successfully:', result);
            } catch (error) {
                tagShareError.textContent = `Failed to share tag: ${error.message}`;
                logDebug('Failed to share tag:', error);
            }
        });
    }

    if (refreshSharesButton) {
        refreshSharesButton.addEventListener('click', fetchSharedContent);
    }

    if (clearCacheButton) {
        clearCacheButton.addEventListener('click', async () => {
            const original = clearCacheButton.textContent;
            clearCacheButton.disabled = true;
            try {
                const res = await apiRequest('/clear_cache', 'POST');
                alert((res && res.detail) || 'Tag membership cache cleared.');
            } catch (error) {
                alert(`Failed to clear cache: ${error.message}`);
            } finally {
                clearCacheButton.disabled = false;
                clearCacheButton.textContent = original;
            }
        });
    }

    async function fetchSharedContent() {
        logDebug('Fetching shared content...');
        try {
            const [videos, tags] = await Promise.all([
                apiRequest('/shared_videos'),
                apiRequest('/shared_tags')
            ]);
            logDebug('Fetched videos:', videos);
            logDebug('Fetched tags:', tags);
            renderSharedVideos(videos);
            renderSharedTags(tags);
        } catch (error) {
            console.error('Failed to fetch shared content:', error.message);
            if(sharedVideosTableBody) sharedVideosTableBody.innerHTML = '<tr><td colspan="4">Failed to load shared videos. Please try again.</td></tr>';
            if(sharedTagsTableBody) sharedTagsTableBody.innerHTML = '<tr><td colspan="4">Failed to load shared tags. Please try again.</td></tr>';
        }
    }

    function renderSharedVideos(videos) {
        if(!sharedVideosTableBody) return;
        sharedVideosTableBody.innerHTML = '';
        if (!videos || videos.length === 0) {
            sharedVideosTableBody.innerHTML = '<tr><td colspan="4">No videos shared yet.</td></tr>';
            return;
        }

        videos.forEach(video => {
            const row = document.createElement('tr');
            const relativeTime = getRelativeTime(video.expires_at);
            const shareUrl = video.share_url;
            const displayName = truncateText(video.video_name, 30);
            const fullName = video.video_name;

            // If the video has a password, append ?pwd=PASSWORD to the copy button's data-url
            let copyUrl = shareUrl;
            const shareId = video.share_id;
            if (video.has_password && sharePasswords[shareId]) {
                copyUrl += (shareUrl.includes('?') ? '&' : '?') + 'pwd=' + encodeURIComponent(sharePasswords[shareId]);
            }

            row.innerHTML = `
                <td title="${escapeHTML(fullName)}">${escapeHTML(displayName)}</td>
                <td>${video.hits}</td>
                <td>${relativeTime}</td>
                <td>
                    <button class="copy-button" data-url="${escapeHTML(copyUrl)}">Copy</button>
                    <button class="edit-button" 
                        data-share-id="${escapeHTML(video.share_id)}" 
                        data-video-name="${escapeHTML(video.video_name.split(' (')[0])}" 
                        data-days-valid="${calculateDaysRemaining(video.expires_at)}" 
                        data-resolution="${escapeHTML(video.resolution)}" 
                        data-has-password="${video.has_password}" 
                        data-show-in-gallery="${video.show_in_gallery}"
                        data-embed-mode="${escapeHTML(video.embed_mode || '')}"
                        data-stash-video-id="${escapeHTML(video.stash_video_id.toString())}">Edit</button>
                    <button class="delete-button" data-share-id="${escapeHTML(video.share_id)}">Delete</button>
                </td>
            `;
            sharedVideosTableBody.appendChild(row);
        });

        addVideoTableButtonListeners();
    }

    function renderSharedTags(tags) {
        if(!sharedTagsTableBody) return;
        sharedTagsTableBody.innerHTML = '';
        if (!tags || tags.length === 0) {
            sharedTagsTableBody.innerHTML = '<tr><td colspan="4">No tags shared yet.</td></tr>';
            return;
        }

        tags.forEach(tag => {
            const row = document.createElement('tr');
            row.dataset.shareId = tag.share_id;
            const relativeTime = getRelativeTime(tag.expires_at);
            const shareUrl = tag.share_url;
            const displayName = truncateText(`${tag.tag_name} (${tag.resolution})`, 30);
            const fullName = `${tag.tag_name} (${tag.resolution})`;

            // If the tag share has a password and we know it (created this
            // session), append ?pwd= so the copied link unlocks it directly.
            let copyUrl = shareUrl;
            if (tag.has_password && sharePasswords[tag.share_id]) {
                copyUrl += (shareUrl.includes('?') ? '&' : '?') + 'pwd=' + encodeURIComponent(sharePasswords[tag.share_id]);
            }

            row.innerHTML = `
                <td title="${escapeHTML(fullName)}"><span class="drag-grip" title="Drag to reorder">⠿</span>${escapeHTML(displayName)}</td>
                <td>${tag.hits}</td>
                <td>${relativeTime}</td>
                <td>
                    <button class="copy-button" data-url="${escapeHTML(copyUrl)}">Copy</button>
                    <button class="edit-tag-button" 
                        data-share-id="${escapeHTML(tag.share_id)}" 
                        data-tag-name="${escapeHTML(tag.tag_name)}" 
                        data-tag-id="${escapeHTML(tag.stash_tag_id)}"
                        data-days-valid="${calculateDaysRemaining(tag.expires_at)}" 
                        data-resolution="${escapeHTML(tag.resolution)}" 
                        data-has-password="${tag.has_password}" 
                        data-show-in-gallery="${tag.show_in_gallery}"
                        data-apply-limit-tag="${tag.apply_limit_tag}"
                        data-gallery-mode="${tag.gallery_mode}"
                        data-embed-mode="${escapeHTML(tag.embed_mode || '')}"
                        data-default-sort="${escapeHTML(tag.default_sort || '')}">Edit</button>
                    <button class="delete-tag-button" data-share-id="${escapeHTML(tag.share_id)}">Delete</button>
                </td>
            `;
            sharedTagsTableBody.appendChild(row);
        });

        addTagTableButtonListeners();
        setupTagReorder();
    }

    // Drag-to-reorder for the Shared Tags list. Reordering the rows updates the
    // order tags appear under "Collections" on the home page.
    let dragRow = null;
    function setupTagReorder() {
        if (!sharedTagsTableBody) return;
        sharedTagsTableBody.querySelectorAll('tr').forEach(row => {
            if (!row.dataset.shareId) return; // skip the "no tags" placeholder row
            row.setAttribute('draggable', 'true');
            row.addEventListener('dragstart', (e) => {
                dragRow = row;
                row.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                try { e.dataTransfer.setData('text/plain', row.dataset.shareId); } catch (_) {}
            });
            row.addEventListener('dragend', () => {
                row.classList.remove('dragging');
                dragRow = null;
                persistTagOrder();
            });
            row.addEventListener('dragover', (e) => {
                e.preventDefault();
                if (!dragRow || dragRow === row) return;
                const rect = row.getBoundingClientRect();
                const before = (e.clientY - rect.top) < rect.height / 2;
                sharedTagsTableBody.insertBefore(dragRow, before ? row : row.nextSibling);
            });
        });
    }

    async function persistTagOrder() {
        const order = Array.from(sharedTagsTableBody.querySelectorAll('tr'))
            .map(r => r.dataset.shareId)
            .filter(Boolean);
        if (!order.length) return;
        try {
            await apiRequest('/reorder_tag_shares', 'PUT', { order });
            logDebug('Saved tag order:', order);
        } catch (err) {
            console.error('Failed to save tag order:', err);
        }
    }

    function addVideoTableButtonListeners() {
        document.querySelectorAll('#shared-videos-table .copy-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const url = e.target.getAttribute('data-url');
                copyToClipboard(url);
            });
        });

        document.querySelectorAll('#shared-videos-table .edit-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const shareId = e.target.getAttribute('data-share-id');
                const videoName = e.target.getAttribute('data-video-name');
                const daysValid = e.target.getAttribute('data-days-valid');
                const resolution = e.target.getAttribute('data-resolution');
                const showInGallery = e.target.getAttribute('data-show-in-gallery') === 'true';
                
                const embedMode = e.target.getAttribute('data-embed-mode') || '';

                if(editShareIdInput) editShareIdInput.value = shareId;
                if(editVideoNameInput) editVideoNameInput.value = videoName;
                if(editDaysValidInput) editDaysValidInput.value = Math.max(1, parseInt(daysValid) || 7);
                if(editResolutionInput) editResolutionInput.value = resolution;
                if(editSharePasswordInput) editSharePasswordInput.value = '';
                if(editShowInGalleryInput) editShowInGalleryInput.checked = showInGallery;
                if(editEmbedModeSelect) editEmbedModeSelect.value = embedMode;
                if(editClearPasswordInput) editClearPasswordInput.checked = false;
                
                if(editModal) editModal.style.display = 'block';
                clearMessages();
            });
        });

        document.querySelectorAll('#shared-videos-table .delete-button').forEach(button => {
            button.addEventListener('click', async (e) => {
                const shareId = e.target.getAttribute('data-share-id');
                if (confirm(`Are you sure you want to delete share ${shareId}?`)) {
                    try {
                        await apiRequest(`/delete_share/${shareId}`, 'DELETE');
                        fetchSharedContent();
                    } catch (error) {
                        alert(`Failed to delete share: ${error.message}`);
                    }
                }
            });
        });
    }

    function addTagTableButtonListeners() {
        document.querySelectorAll('#shared-tags-table .copy-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const url = e.target.getAttribute('data-url');
                copyToClipboard(url);
            });
        });

        document.querySelectorAll('#shared-tags-table .edit-tag-button').forEach(button => {
            button.addEventListener('click', (e) => {
                const t = e.target;
                if(editTagShareIdInput) editTagShareIdInput.value = t.getAttribute('data-share-id');
                if(editTagNameInput) editTagNameInput.value = t.getAttribute('data-tag-name') || '';
                if(editTagIdInput) editTagIdInput.value = t.getAttribute('data-tag-id') || '';
                if(editTagDaysValidInput) editTagDaysValidInput.value = Math.max(1, parseInt(t.getAttribute('data-days-valid')) || 7);
                if(editTagResolutionInput) editTagResolutionInput.value = t.getAttribute('data-resolution') || 'MEDIUM';
                if(editTagEmbedModeSelect) editTagEmbedModeSelect.value = t.getAttribute('data-embed-mode') || '';
                if(editTagDefaultSortSelect) editTagDefaultSortSelect.value = t.getAttribute('data-default-sort') || '';
                if(editTagSharePasswordInput) editTagSharePasswordInput.value = '';
                if(editTagShowInGalleryInput) editTagShowInGalleryInput.checked = t.getAttribute('data-show-in-gallery') === 'true';
                if(editTagApplyLimitTagInput) editTagApplyLimitTagInput.checked = t.getAttribute('data-apply-limit-tag') === 'true';
                if(editTagGalleryModeInput) editTagGalleryModeInput.checked = t.getAttribute('data-gallery-mode') === 'true';
                if(editTagClearPasswordInput) editTagClearPasswordInput.checked = false;
                syncEditApplyLimitState();

                if(editTagModal) editTagModal.style.display = 'block';
                clearMessages();
            });
        });

        document.querySelectorAll('#shared-tags-table .delete-tag-button').forEach(button => {
            button.addEventListener('click', async (e) => {
                const shareId = e.target.getAttribute('data-share-id');
                if (confirm(`Are you sure you want to delete tag share ${shareId}?`)) {
                    try {
                        await apiRequest(`/delete_tag_share/${shareId}`, 'DELETE');
                        fetchSharedContent();
                    } catch (error) {
                        alert(`Failed to delete tag share: ${error.message}`);
                    }
                }
            });
        });
    }
    
    if (cancelEditButton) {
        cancelEditButton.addEventListener('click', () => {
            if(editModal) editModal.style.display = 'none';
        });
    }

    if (saveEditButton) {
        saveEditButton.addEventListener('click', async () => {
            clearMessages();
            const shareId = editShareIdInput.value;
            
            const editButton = document.querySelector(`#shared-videos-table button.edit-button[data-share-id="${shareId}"]`);
            const stashVideoId = editButton ? parseInt(editButton.getAttribute('data-stash-video-id')) : 0;
            
            const updatedData = {
                video_name: editVideoNameInput.value,
                stash_video_id: stashVideoId, 
                days_valid: parseInt(editDaysValidInput.value, 10),
                resolution: editResolutionInput.value,
                password: editSharePasswordInput.value || null,
                show_in_gallery: editShowInGalleryInput.checked,
                embed_mode: (editEmbedModeSelect && editEmbedModeSelect.value) || null,
                clear_password: editClearPasswordInput ? editClearPasswordInput.checked : false
            };

            if (!updatedData.video_name || isNaN(updatedData.days_valid) || !updatedData.resolution) {
                editError.textContent = 'Please fill in all required fields correctly.';
                return;
            }

            try {
                await apiRequest(`/edit_share/${shareId}`, 'PUT', updatedData);
                // Keep the session password map in sync so the Copy link's ?pwd=
                // reflects the edit (a new password set, or a cleared one).
                if (updatedData.clear_password) {
                    delete sharePasswords[shareId];
                } else if (updatedData.password) {
                    sharePasswords[shareId] = updatedData.password;
                }
                if(editModal) editModal.style.display = 'none';
                fetchSharedContent();
            } catch (error) {
                editError.textContent = `Failed to update share: ${error.message}`;
            }
        });
    }

    if (cancelEditTagButton) {
        cancelEditTagButton.addEventListener('click', () => {
            if(editTagModal) editTagModal.style.display = 'none';
        });
    }

    if (saveEditTagButton) {
        saveEditTagButton.addEventListener('click', async () => {
            clearMessages();
            const shareId = editTagShareIdInput.value;

            const updatedData = {
                tag_name: editTagNameInput.value,
                tag_id: editTagIdInput.value,
                days_valid: parseInt(editTagDaysValidInput.value, 10),
                resolution: editTagResolutionInput.value,
                password: editTagSharePasswordInput.value || null,
                show_in_gallery: editTagShowInGalleryInput.checked,
                apply_limit_tag: editTagApplyLimitTagInput ? editTagApplyLimitTagInput.checked : true,
                gallery_mode: editTagGalleryModeInput ? editTagGalleryModeInput.checked : false,
                embed_mode: (editTagEmbedModeSelect && editTagEmbedModeSelect.value) || null,
                default_sort: (editTagDefaultSortSelect && editTagDefaultSortSelect.value) || null,
                clear_password: editTagClearPasswordInput ? editTagClearPasswordInput.checked : false
            };

            if (!updatedData.tag_name || !updatedData.tag_id || isNaN(updatedData.days_valid) || !updatedData.resolution) {
                editTagError.textContent = 'Please fill in all required fields correctly.';
                return;
            }

            try {
                await apiRequest(`/edit_tag_share/${shareId}`, 'PUT', updatedData);
                // Keep the session password map in sync so the Copy link's ?pwd=
                // reflects the edit (a new password set, or a cleared one).
                if (updatedData.clear_password) {
                    delete sharePasswords[shareId];
                } else if (updatedData.password) {
                    sharePasswords[shareId] = updatedData.password;
                }
                if(editTagModal) editTagModal.style.display = 'none';
                fetchSharedContent();
            } catch (error) {
                editTagError.textContent = `Failed to update tag share: ${error.message}`;
            }
        });
    }

    // Get base domain from shared videos response
    async function setBaseDomain() {
        try {
            const response = await fetch(backendApiBase + '/shared_videos', {
                headers: {
                    'Authorization': `Bearer ${localStorage.getItem('share_token')}`
                }
            });
            if (response.ok) {
                const videos = await response.json();
                if (videos.length > 0 && videos[0].share_url) {
                    // Extract base domain from share URL
                    const shareUrl = new URL(videos[0].share_url);
                    const baseDomain = shareUrl.origin;
                    const logoLink = document.getElementById('logo-link');
                    if (logoLink) {
                        logoLink.href = baseDomain;
                    }
                }
            }
        } catch (error) {
            console.log('Could not determine base domain:', error);
        }
    }

    // Load site configuration
    async function loadSiteConfig() {
        try {
            const data = await apiRequest('/site_config', 'GET', null, false);
            if (data) {
                if (data.site_name) {
                    const siteNameElement = document.getElementById('site-name');
                    if (siteNameElement) {
                        siteNameElement.textContent = data.site_name;
                    }
                    document.title = `Admin Panel - ${data.site_name}`;
                }
                if (data.base_domain) {
                    const logoLink = document.getElementById('logo-link');
                    if (logoLink) {
                        logoLink.href = data.base_domain;
                    }
                }
                if (data.logo_path) {
                    const logoImg = document.querySelector('#logo-link img.logo');
                    if (logoImg) {
                        // Use the server-resolved logo (same logic as public pages)
                        // instead of the brittle svg-only onerror fallback.
                        logoImg.onerror = null;
                        logoImg.src = data.logo_path;
                        logoImg.srcset = data.logo_srcset || '';
                    }
                }
            }
        } catch (error) {
            console.log('Could not load site config:', error);
        }
    }

    // Load site config on page load
    loadSiteConfig();

    // --- Tab switching logic for 2-pane layout ---
    function setupTabs(tabBtnIds, tabContentIds) {
        tabBtnIds.forEach((btnId, idx) => {
            const btn = document.getElementById(btnId);
            const content = document.getElementById(tabContentIds[idx]);
            if (btn && content) {
                btn.addEventListener('click', () => {
                    // Deactivate all
                    tabBtnIds.forEach((otherBtnId, j) => {
                        const otherBtn = document.getElementById(otherBtnId);
                        const otherContent = document.getElementById(tabContentIds[j]);
                        if (otherBtn) otherBtn.classList.remove('active');
                        if (otherContent) otherContent.style.display = 'none';
                    });
                    // Activate this
                    btn.classList.add('active');
                    content.style.display = '';
                });
            }
        });
    }
    setupTabs(['tab-share-video', 'tab-share-tag'], ['tab-content-share-video', 'tab-content-share-tag']);
    setupTabs(['tab-list-videos', 'tab-list-tags'], ['tab-content-list-videos', 'tab-content-list-tags']);
});
