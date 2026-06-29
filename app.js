// Global Variables
const familyModal = document.getElementById('family-member-modal');
let memories = [];  // Clear on load, don't load from localStorage
let isRecording = false;
let isVideoRecording = false;
let audioRecorder = null;
let videoRecorder = null;
let audioChunks = [];
let videoChunks = [];
let recordedVideos = [];
let currentStream = null; // Store the current stream
let recordingStartTime = null;
let transmissionQueue = [];
let videoTimerInterval = null;

// Family Members Data
let familyMembers = JSON.parse(localStorage.getItem('familyMembers')) || [];
console.log('Loaded familyMembers from localStorage on page load:', familyMembers);
let familyTreeView = true;
let familyId = localStorage.getItem('familyId') || null;
let inviteCode = localStorage.getItem('inviteCode') || null;
let isSharedTree = localStorage.getItem('isSharedTree') === 'true';
let hasJoins = localStorage.getItem('hasJoins') === 'true';

// Add autobiography field to existing family members if missing
familyMembers.forEach(member => {
if (!member.autobiography) {
member.autobiography = `This is the life story of ${member.name}...`;
}
});

// Update shared badge visibility
function updateSharedBadge() {
const badge = document.getElementById('shared-badge');
if (!badge) return;

// Hide badge for new lineages (no family_id or created via Start New Lineage)
if (!familyId || !isSharedTree) {
badge.style.display = 'none';
return;
}

// Show "Shareable" (gray) if invite code exists but no joins
if (inviteCode && !hasJoins) {
badge.textContent = 'Shareable';
badge.classList.add('shareable');
badge.style.display = 'inline-block';
return;
}

// Show "Shared" (emerald) if tree was joined via referral or has joins
if (isSharedTree || hasJoins) {
badge.textContent = 'Shared';
badge.classList.remove('shareable');
badge.style.display = 'inline-block';
return;
}

// Default: hide
badge.style.display = 'none';
}

// Mock shared tree data
const mockSharedTree = [
{
id: 'shared-1',
name: 'Grandfather John',
relationship: 'grandfather',
birthYear: 1945,
isAlive: false,
autobiography: 'John was a hardworking man who built his family from nothing. He served in the military and later started a small business that supported three generations.',
photo: null
},
{
id: 'shared-2',
name: 'Grandmother Mary',
relationship: 'grandmother',
birthYear: 1948,
isAlive: false,
parentId: 'shared-1',
autobiography: 'Mary was the heart of the family. She was known for her cooking and her endless stories of the old country.',
photo: null
},
{
id: 'shared-3',
name: 'Father Robert',
relationship: 'father',
birthYear: 1970,
isAlive: true,
parentId: 'shared-1',
autobiography: 'Robert followed in his father\'s footsteps but expanded the business. He is proud of his heritage and works hard to provide for his family.',
photo: null
},
{
id: 'shared-4',
name: 'Mother Susan',
relationship: 'mother',
birthYear: 1972,
isAlive: true,
spouseOf: 'shared-3',
autobiography: 'Susan is a teacher who has dedicated her life to education. She met Robert in college and they have been together ever since.',
photo: null
},
{
id: 'shared-5',
name: 'Uncle Michael',
relationship: 'uncle',
birthYear: 1975,
isAlive: true,
parentId: 'shared-1',
autobiography: 'Michael is the adventurous one in the family. He traveled the world before settling down to start his own tech company.',
photo: null
}
];

// Private Stories Data
let privateStories = JSON.parse(localStorage.getItem('privateStories')) || [];
let storiesAccessView = true; // true = my stories, false = accessible stories

// Story Recording Variables
let storyAudioRecorder = null;
let storyVideoRecorder = null;
let storyAudioChunks = [];
let storyVideoChunks = [];
let storyRecordingStartTime = null;
let storyAudioTimerInterval = null;
let storyVideoTimerInterval = null;
let storyCurrentAudioBlob = null;
let storyCurrentVideoBlob = null;

// Time Capsule Recording Variables
let capsuleAudioRecorder = null;
let capsuleVideoRecorder = null;
let capsuleAudioChunks = [];
let capsuleVideoChunks = [];
let capsuleRecordingStartTime = null;
let capsuleAudioTimerInterval = null;
let capsuleVideoTimerInterval = null;
let capsuleCurrentAudioBlob = null;
let capsuleCurrentVideoBlob = null;

// Story Prompts
const storyPrompts = [
"Tell me about your grandmother...",
"What's your favorite childhood memory?",
"Describe a moment that changed your life...",
"What advice would you give your younger self?",
"Tell me about your first love...",
"What are you most proud of?",
"Describe your perfect day...",
"What makes you truly happy?",
"Tell me about a time you overcame fear...",
"What legacy do you want to leave behind?"
];

// Family Tree Functions
function loadFamilyTree() {
renderFamilyTree();
}

function initDraggableTree() {
const container = document.getElementById('family-tree-container');
const canvas = document.getElementById('tree-canvas');

if (!container || !canvas) return;

let isDragging = false;
let startX, startY, scrollLeft, scrollTop;

container.addEventListener('mousedown', (e) => {
isDragging = true;
startX = e.pageX - container.offsetLeft;
startY = e.pageY - container.offsetTop;
scrollLeft = container.scrollLeft;
scrollTop = container.scrollTop;
});

container.addEventListener('mouseleave', () => {
isDragging = false;
});

container.addEventListener('mouseup', () => {
isDragging = false;
});

container.addEventListener('mousemove', (e) => {
if (!isDragging) return;
e.preventDefault();
const x = e.pageX - container.offsetLeft;
const y = e.pageY - container.offsetTop;
const walkX = (x - startX) * 2;
const walkY = (y - startY) * 2;
container.scrollLeft = scrollLeft - walkX;
container.scrollTop = scrollTop - walkY;
});
}

function renderFamilyTree() {
const container = document.getElementById('family-tree-container');

// Update shared badge
updateSharedBadge();

if (familyMembers.length === 0) {
container.innerHTML = `
<div style="text-align: center; color: var(--text-secondary); padding: 40px;">
<h3 style="margin-bottom: 20px;">No Family Members Yet</h3>
<p style="margin-bottom: 20px;">Start building your family tree by adding family members.</p>
<div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap;">
<button class="btn" onclick="openFamilyMemberModal()">
<span>+</span> Start New Lineage
</button>
<button class="btn btn-secondary" onclick="openReferralCodeModal()">
<span>🔗</span> Join via Code
</button>
</div>
</div>
`;
return;
}

if (familyTreeView) {
renderTreeVisualization(container);
} else {
renderListView(container);
}
}

function renderTreeVisualization(container) {
console.log('renderTreeVisualization called with familyMembers:', familyMembers);
// Clear container before redrawing
container.innerHTML = '';

// Build tree hierarchy
const roots = familyMembers.filter(member => !member.parentId);
console.log('Found roots (members without parentId):', roots);

// Check if Laurel exists and ensure she's treated as root
const laurel = familyMembers.find(m => m.name.toLowerCase() === 'laurel');
if (laurel && !roots.includes(laurel)) {
// If Laurel exists but has a parent, remove her parent to make her root
laurel.parentId = null;
roots.push(laurel);
console.log('Laurel found and made root');
}

if (roots.length === 0 && familyMembers.length > 0) {
// If no roots found, treat first member as root
roots.push(familyMembers[0]);
console.log('No roots found, treating first member as root:', familyMembers[0]);
}

let html = '<div class="tree-canvas" id="tree-canvas">';

roots.forEach(root => {
html += renderTreeNode(root);
});

html += '</div>';
container.innerHTML = html;
console.log('Tree rendered to container');

// Initialize draggable functionality
initDraggableTree();
}

function renderTreeNode(member, generation = 0) {
const children = familyMembers.filter(child => child.parentId === member.id);
const age = member.birthYear ? new Date().getFullYear() - member.birthYear : '';
const photoHtml = member.photo ? 
`<img src="${member.photo}" onerror="this.style.display='none'">` :
`<div>${member.name.charAt(0)}</div>`;

let html = `
<div class="tree-node" data-generation="${generation}">
<div class="tree-member-card" onclick="viewFamilyMember('${member.id}')">
<div class="tree-member-avatar">
${photoHtml}
</div>
<div class="tree-member-name">${member.name}</div>
<div class="tree-member-relationship">${member.relationship}</div>
${age ? `<div style="font-size: 0.7rem; color: var(--text-secondary);">Age: ${age}</div>` : ''}
<div style="display: flex; gap: 8px; margin-top: 10px; justify-content: center; flex-wrap: wrap;">
<button class="tree-autobiography-btn" onclick="event.stopPropagation(); console.log('Edit button clicked for member:', '${member.id}'); openFamilyMemberModal('${member.id}')" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%);">
Edit
</button>
<button class="tree-add-relative-btn" onclick="event.stopPropagation(); console.log('+ button clicked for member:', '${member.id}'); toggleAddRelativeDropdown('${member.id}', event)">
+
</button>
<div id="add-relative-dropdown-${member.id}" class="add-relative-dropdown" style="display: none;">
<button class="dropdown-item" data-relationship="parent" data-member-id="${member.id}" onclick="event.stopPropagation(); event.preventDefault(); initiateAddMember('${member.id}', 'parent'); return false;">Add Parent</button>
<button class="dropdown-item" data-relationship="sibling" data-member-id="${member.id}" onclick="event.stopPropagation(); event.preventDefault(); initiateAddMember('${member.id}', 'sibling'); return false;">Add Sibling</button>
<button class="dropdown-item" data-relationship="child" data-member-id="${member.id}" onclick="event.stopPropagation(); event.preventDefault(); initiateAddMember('${member.id}', 'child'); return false;">Add Child</button>
<button class="dropdown-item" data-relationship="spouse" data-member-id="${member.id}" onclick="event.stopPropagation(); event.preventDefault(); initiateAddMember('${member.id}', 'spouse'); return false;">Add Spouse</button>
</div>
</div>
</div>
`;

if (children.length > 0) {
html += '<div class="tree-children">';
children.forEach(child => {
html += '<div class="tree-child">';
html += renderTreeNode(child, generation + 1);
html += '</div>';
});
html += '</div>';
}

html += '</div>';
return html;
}

function renderTreeView(container) {
let html = '<div class="family-tree-grid">';

familyMembers.forEach(member => {
const age = member.birthYear ? new Date().getFullYear() - member.birthYear : '';
const photoHtml = member.photo ? 
`<img src="${member.photo}" onerror="this.style.display='none'">` :
`<div>${member.name.charAt(0)}</div>`;

html += `
<div class="family-member-card" onclick="viewFamilyMember('${member.id}')">
<div class="family-member-avatar">
${photoHtml}
</div>
<div class="family-member-name">${member.name}</div>
<div class="family-member-relationship">${member.relationship}</div>
${age ? `<div class="family-member-age">Age: ${age}</div>` : ''}
<button class="autobiography-btn" onclick="event.stopPropagation(); viewAutobiography('${member.id}')">
View Autobiography
</button>
</div>
`;
});

html += '</div>';
container.innerHTML = html;
}

function renderListView(container) {
let html = '<div class="family-tree-list">';

familyMembers.forEach(member => {
const age = member.birthYear ? new Date().getFullYear() - member.birthYear : '';
const photoHtml = member.photo ? 
`<img src="${member.photo}" onerror="this.style.display='none'">` :
`<div>${member.name.charAt(0)}</div>`;

html += `
<div class="family-list-item" onclick="viewFamilyMember('${member.id}')">
<div class="family-list-avatar">
${photoHtml}
</div>
<div class="family-list-info">
<div class="family-member-name">${member.name}</div>
<div class="family-member-relationship">${member.relationship}</div>
${member.bio ? `<div style="font-size: 0.8rem; color: var(--text-secondary); font-style: italic; margin-top: 5px;">${member.bio.substring(0, 100)}${member.bio.length > 100 ? '...' : ''}</div>` : ''}
</div>
<div style="text-align: right;">
${age ? `<div class="family-member-age" style="margin-bottom: 10px;">Age: ${age}</div>` : ''}
<div style="display: flex; gap: 8px; justify-content: flex-end;">
<button class="autobiography-btn" onclick="event.stopPropagation(); viewAutobiography('${member.id}')">
View Autobiography
</button>
<button class="autobiography-btn" onclick="event.stopPropagation(); openFamilyMemberModal('${member.id}')" style="background: linear-gradient(135deg, #10b981 0%, #059669 100%);">
Edit
</button>
</div>
</div>
</div>
`;
});

html += '</div>';
container.innerHTML = html;
}

function toggleTreeView() {
familyTreeView = !familyTreeView;
renderFamilyTree();
}

function openFamilyMemberModal(memberId = null) {
console.log('Mode:', memberId ? 'EDIT' : 'ADD', '| Source:', memberId || 'N/A', '| Relationship: N/A');
const form = document.getElementById('family-member-form');
const modal = familyModal || document.getElementById('family-member-modal');
const deleteBtn = document.getElementById('delete-member-btn');
const modalTitle = modal ? modal.querySelector('.modal-title') : null;
const submitBtn = form ? form.querySelector('button[type="submit"]') : null;

if (!modal) {
console.error('Family member modal not found!');
return;
}

// STRICT RESET - Clear everything first
if (form) form.reset();
const idField = document.getElementById('family-member-id');
if (idField) idField.value = '';
const nameField = document.getElementById('family-member-name');
if (nameField) nameField.value = '';
const birthYearField = document.getElementById('family-member-birth-year');
if (birthYearField) birthYearField.value = '';
const photoUrlField = document.getElementById('family-member-photo-url');
if (photoUrlField) photoUrlField.value = '';
const bioField = document.getElementById('family-member-bio');
if (bioField) bioField.value = '';
const linkedAccountField = document.getElementById('family-member-linked-account');
if (linkedAccountField) linkedAccountField.value = '';
const aliveField = document.getElementById('family-member-alive');
if (aliveField) aliveField.value = 'true';

const preview = document.getElementById('profile-photo-preview');
if (preview) preview.style.display = 'none';
const previewImg = document.getElementById('profile-photo-preview-img');
if (previewImg) previewImg.src = '';

const siblingField = document.getElementById('sibling-related-field');
if (siblingField) siblingField.style.display = 'none';
const spouseField = document.getElementById('spouse-related-field');
if (spouseField) spouseField.style.display = 'none';
const accountLinkingField = document.getElementById('account-linking-group');
if (accountLinkingField) accountLinkingField.style.display = 'none';

// Show/hide delete button based on whether we're editing
if (deleteBtn) deleteBtn.style.display = memberId ? 'block' : 'none';

// Populate parent dropdown
const parentSelect = document.getElementById('family-member-parent');
if (parentSelect) {
parentSelect.innerHTML = '<option value="">No parent (root of tree)</option>';

familyMembers.forEach(member => {
if (memberId !== member.id) { // Don't allow self as parent
parentSelect.innerHTML += `<option value="${member.id}">${member.name}</option>`;
}
});
}

if (memberId) {
// Edit existing member
const member = familyMembers.find(m => m.id === memberId);
if (member) {
if (idField) idField.value = member.id;
if (nameField) nameField.value = member.name;
if (parentSelect) parentSelect.value = member.parentId || '';
const relationshipSelect = document.getElementById('family-member-relationship');
if (relationshipSelect) relationshipSelect.value = member.relationship;
if (birthYearField) birthYearField.value = member.birthYear || '';
if (photoUrlField) photoUrlField.value = member.photo || '';
if (bioField) bioField.value = member.bio || '';
if (aliveField) aliveField.value = member.isAlive !== undefined ? member.isAlive.toString() : 'true';
if (linkedAccountField) linkedAccountField.value = member.linkedAccount || '';

// Update title and button for edit mode
if (modalTitle) modalTitle.textContent = `Edit ${member.name}'s Profile`;
if (submitBtn) submitBtn.textContent = 'Save Changes';

// Toggle account linking based on alive status
toggleAccountLinking();

// Handle sibling relationship
if (member.relationship === 'sibling' && member.siblingOf) {
const siblingOfSelect = document.getElementById('family-member-sibling-of');
if (siblingOfSelect) {
siblingOfSelect.innerHTML = '<option value="">Select sibling...</option>';
familyMembers.forEach(m => {
if (m.id !== member.id) {
siblingOfSelect.innerHTML += `<option value="${m.id}">${m.name}</option>`;
}
});
siblingOfSelect.value = member.siblingOf;
if (siblingField) siblingField.style.display = 'block';
}
}

// Handle spouse relationship
if (member.relationship === 'spouse' && member.spouseOf) {
const spouseOfSelect = document.getElementById('family-member-spouse-of');
if (spouseOfSelect) {
spouseOfSelect.innerHTML = '<option value="">Select spouse...</option>';
familyMembers.forEach(m => {
if (m.id !== member.id) {
spouseOfSelect.innerHTML += `<option value="${m.id}">${m.name}</option>`;
}
});
spouseOfSelect.value = member.spouseOf;
if (spouseField) spouseField.style.display = 'block';
}
}

// Show profile picture preview if exists
if (member.photo) {
if (preview) preview.style.display = 'block';
if (previewImg) previewImg.src = member.photo;
}
}
} else {
// Add new member - form already reset above
// Set default title and button for add mode
if (modalTitle) modalTitle.textContent = 'Add New Family Member';
if (submitBtn) submitBtn.textContent = 'Add to Tree';
}

modal.classList.add('active');
console.log('Modal should be visible now');
}

function openAddModal(sourceMemberId, relationship) {
console.log('Mode: ADD | Source:', sourceMemberId, '| Relationship:', relationship);

// Set context for the modal
const member = familyMembers.find(m => m.id === sourceMemberId);
if (!member) {
console.error('Source member not found:', sourceMemberId);
return;
}

let defaultParentId = '';
let defaultRelationship = relationship;
let defaultSiblingOf = '';
let defaultSpouseOf = '';

switch (relationship) {
case 'parent':
defaultParentId = member.parentId || '';
defaultRelationship = 'parent';
break;
case 'sibling':
defaultSiblingOf = sourceMemberId;
defaultRelationship = 'sibling';
defaultParentId = member.parentId || '';
break;
case 'child':
defaultParentId = sourceMemberId;
defaultRelationship = 'child';
break;
case 'spouse':
defaultSpouseOf = sourceMemberId;
if (member.relationship === 'parent') {
defaultRelationship = 'co-parent';
} else if (member.relationship === 'grandparent') {
defaultRelationship = 'co-grandparent';
} else {
defaultRelationship = 'spouse';
}
defaultParentId = member.parentId || '';
break;
}

window.addRelativeContext = {
parentId: defaultParentId,
relationship: defaultRelationship,
siblingOf: defaultSiblingOf,
spouseOf: defaultSpouseOf,
sourceMemberName: member.name
};

// Open modal without memberId (ADD mode)
openFamilyMemberModal();
}

function openEditModal(memberId) {
console.log('Mode: EDIT | Source:', memberId, '| Relationship: N/A');
openFamilyMemberModal(memberId);
}

function closeFamilyMemberModal() {
document.getElementById('family-member-modal').classList.remove('active');
document.getElementById('family-member-form').reset();

// Reset profile picture preview
document.getElementById('profile-photo-preview').style.display = 'none';
document.getElementById('profile-photo-preview-img').src = '';
}

// Delete Member Functions
function openDeleteConfirmationModal() {
const memberId = document.getElementById('family-member-id').value;
const member = familyMembers.find(m => m.id === memberId);

if (!member) return;

document.getElementById('delete-member-name').textContent = member.name;
document.getElementById('delete-confirmation-modal').classList.add('active');
}

function closeDeleteConfirmationModal() {
document.getElementById('delete-confirmation-modal').classList.remove('active');
}

function confirmDeleteMember() {
const memberId = document.getElementById('family-member-id').value;
const member = familyMembers.find(m => m.id === memberId);

if (!member) return;

// Get the member's parent before deletion
const memberParentId = member.parentId;

// Remove the member from the array
familyMembers = familyMembers.filter(m => m.id !== memberId);

// Heal the tree: update children's parentId to the deleted member's parent
familyMembers.forEach(m => {
if (m.parentId === memberId) {
m.parentId = memberParentId || null;
}
// Clear siblingOf and spouseOf pointers
if (m.siblingOf === memberId) {
m.siblingOf = null;
}
if (m.spouseOf === memberId) {
m.spouseOf = null;
}
});

// Save to localStorage
saveFamilyMembers();

// Close modals
closeDeleteConfirmationModal();
closeFamilyMemberModal();

// Re-render tree
renderFamilyTree();

showSuccess(`${member.name} has been removed from the family tree.`);
}

function toggleAccountLinking() {
const isAlive = document.getElementById('family-member-alive').value === 'true';
const accountLinkingGroup = document.getElementById('account-linking-group');
accountLinkingGroup.style.display = isAlive ? 'block' : 'none';
}

function goToProfile() {
window.location.href = '/app/profile.html';
}

function logout() {
if (confirm('Are you sure you want to logout?')) {
// Clear all application state
memories = [];
localStorage.removeItem('userProfile');
localStorage.removeItem('userEmail');
localStorage.removeItem('userName');
localStorage.removeItem('timeCapsuleMemories');
localStorage.removeItem('familyMembers');
localStorage.removeItem('privateStories');
window.location.href = '/';
}
}

function viewFamilyMember(memberId) {
console.log('viewFamilyMember called - this opens EDIT modal for member:', memberId);
openFamilyMemberModal(memberId);
}

function viewAutobiography(memberId) {
const member = familyMembers.find(m => m.id === memberId);
if (!member) return;

const modal = document.getElementById('autobiography-modal');
const title = document.getElementById('autobiography-title');
const content = document.getElementById('autobiography-content');

title.textContent = `${member.name}'s Autobiography`;

const autobiography = member.autobiography || 'No autobiography written yet.';

let publicMessagesHtml = '';
if (member.linkedAccount && member.isAlive) {
publicMessagesHtml = `
<div style="margin-top: 40px; padding-top: 30px; border-top: 1px solid rgba(74, 158, 255, 0.2);">
<h3 style="margin-bottom: 20px;">📢 Public Messages</h3>
<div id="public-messages-container" style="display: flex; flex-direction: column; gap: 15px;">
<div style="text-align: center; color: var(--text-secondary);">Loading public messages...</div>
</div>
</div>
`;
// Load public messages after modal opens
setTimeout(() => loadPublicMessages(member.linkedAccount), 100);
}

content.innerHTML = `
<div style="margin-bottom: 30px;">
<div style="display: flex; align-items: center; gap: 20px; margin-bottom: 20px;">
${member.photo ? 
`<img src="${member.photo}" style="width: 50px; height: 50px; border-radius: 50%; object-fit: cover;" onerror="this.style.display='none'">` :
`<div style="width: 50px; height: 50px; border-radius: 50%; background: var(--accent-color); display: flex; align-items: center; justify-content: center; font-size: 1.2rem; font-weight: 700; color: white;">${member.name.charAt(0)}</div>`
}
<div>
<h2 style="margin: 0;">${member.name}</h2>
<div style="color: var(--text-secondary); font-size: 0.9rem;">${member.relationship || 'Family Member'}</div>
</div>
</div>

<div style="background: rgba(255,255,255,0.03); padding: 20px; border-radius: var(--border-radius); border: 1px solid rgba(255,255,255,0.1);">
<p style="line-height: 1.8; color: var(--text-primary); white-space: pre-wrap;">${autobiography}</p>
</div>

${member.linkedAccount && member.isAlive ? `
<div style="margin-top: 20px; text-align: center;">
<button class="btn btn-secondary" onclick="editAutobiography('${member.id}')">Edit Autobiography</button>
</div>
` : ''}
</div>

${publicMessagesHtml}
`;

modal.classList.add('active');
}

async function loadPublicMessages(email) {
const container = document.getElementById('public-messages-container');
if (!container) return;

try {
const response = await fetch(`http://localhost:5000/api/memories/public/${email}`);
const data = await response.json();

if (data.memories && data.memories.length > 0) {
container.innerHTML = data.memories.map(memory => `
<div style="background: rgba(30, 41, 59, 0.9); border: 1px solid rgba(74, 158, 255, 0.3); border-radius: 12px; padding: 20px;">
<h4 style="margin: 0 0 10px 0; color: var(--accent-color);">${memory.title}</h4>
<p style="margin: 0; line-height: 1.6; color: var(--text-secondary);">${memory.content}</p>
<div style="margin-top: 10px; font-size: 0.8rem; color: var(--text-secondary);">
${memory.visibility === 'public' ? '🌐 Public' : '👨‍👩‍👧 Family'} • ${new Date(memory.created_at).toLocaleDateString()}
</div>
</div>
`).join('');
} else {
container.innerHTML = '<div style="text-align: center; color: var(--text-secondary);">No public messages found</div>';
}
} catch (error) {
console.error('Error loading public messages:', error);
container.innerHTML = '<div style="text-align: center; color: var(--text-secondary);">Unable to load public messages</div>';
}
}

function closeAutobiographyModal() {
document.getElementById('autobiography-modal').classList.remove('active');
}

function viewRelatedMemories(memberId) {
// Switch to My Vault tab and filter memories by this family member
switchTab('vault');
// TODO: Implement filtering logic to show only memories related to this member
showSuccess(`Loading memories for family member...`);
}

function editAutobiography(memberId) {
const member = familyMembers.find(m => m.id === memberId);
if (!member) return;

const content = document.getElementById('autobiography-content');

content.innerHTML = `
<div style="margin-bottom: 20px;">
<h4 style="color: var(--text-primary); margin-bottom: 10px;">Edit Autobiography for ${member.name}</h4>
<textarea id="autobiography-editor" style="width: 100%; min-height: 300px; padding: 15px; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); border-radius: var(--border-radius); color: var(--text-primary); resize: vertical;" placeholder="Write your autobiography here...">${member.autobiography || ''}</textarea>
</div>

<div style="text-align: center;">
<button class="btn btn-secondary" onclick="closeAutobiographyModal()" style="margin-right: 10px;">Cancel</button>
<button class="btn" onclick="saveAutobiography('${memberId}')">Save Autobiography</button>
</div>
`;
}

function saveAutobiography(memberId) {
const member = familyMembers.find(m => m.id === memberId);
if (!member) return;

const autobiographyText = document.getElementById('autobiography-editor').value;
member.autobiography = autobiographyText;

saveFamilyMembers();
showSuccess('Autobiography saved successfully!');
viewAutobiography(memberId);
}

function saveFamilyMembers() {
localStorage.setItem('familyMembers', JSON.stringify(familyMembers));
}

// Referral System Functions
function openReferralCodeModal() {
document.getElementById('referral-code-modal').classList.add('active');
document.getElementById('referral-code-input').value = '';
}

function closeReferralCodeModal() {
document.getElementById('referral-code-modal').classList.remove('active');
}

function joinFamilyTree() {
const codeInput = document.getElementById('referral-code-input');
const code = codeInput.value.trim().toUpperCase();

if (code.length !== 8) {
showError('Please enter a valid 8-character referral code.');
return;
}

// Generate a family ID from the code (mock logic)
const newFamilyId = 'FAM-' + code;

// Save to localStorage
localStorage.setItem('familyId', newFamilyId);
familyId = newFamilyId;

// Mark as shared tree (joined via referral)
localStorage.setItem('isSharedTree', 'true');
isSharedTree = true;

// Generate an invite code
const newInviteCode = generateRandomCode();
localStorage.setItem('inviteCode', newInviteCode);
inviteCode = newInviteCode;

// Load mock shared tree data
familyMembers = [...mockSharedTree];
saveFamilyMembers();

// Close modal and refresh
closeReferralCodeModal();
renderFamilyTree();
showSuccess('Successfully joined the family tree!');
}

function openFamilyTreeSettings() {
// Generate family ID if doesn't exist
if (!familyId) {
familyId = 'FAM-' + generateRandomCode();
localStorage.setItem('familyId', familyId);
}

// Generate invite code if doesn't exist
if (!inviteCode) {
inviteCode = generateRandomCode();
localStorage.setItem('inviteCode', inviteCode);
}

document.getElementById('family-id-display').value = familyId;
document.getElementById('invite-code-display').value = inviteCode;
document.getElementById('family-tree-settings-modal').classList.add('active');
}

function closeFamilyTreeSettings() {
document.getElementById('family-tree-settings-modal').classList.remove('active');
}

function generateInviteCode() {
const newCode = generateRandomCode();
inviteCode = newCode;
localStorage.setItem('inviteCode', newCode);
document.getElementById('invite-code-display').value = newCode;
showSuccess('New invite code generated!');
}

function copyFamilyId() {
const familyIdDisplay = document.getElementById('family-id-display');
navigator.clipboard.writeText(familyIdDisplay.value).then(() => {
showSuccess('Family ID copied to clipboard!');
}).catch(() => {
showError('Failed to copy Family ID');
});
}

function copyInviteCode() {
const inviteCodeDisplay = document.getElementById('invite-code-display');
navigator.clipboard.writeText(inviteCodeDisplay.value).then(() => {
showSuccess('Invite code copied to clipboard!');
}).catch(() => {
showError('Failed to copy invite code');
});
}

function generateRandomCode() {
const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
let code = '';
for (let i = 0; i < 8; i++) {
code += chars.charAt(Math.floor(Math.random() * chars.length));
}
// Format as XXXX-XXXX
return code.substring(0, 4) + '-' + code.substring(4);
}

// Override openFamilyMemberModal to initialize family ID for new lineages
const originalOpenFamilyMemberModal = openFamilyMemberModal;
openFamilyMemberModal = function(memberId) {
// If starting new lineage (no members and no family ID), create one
if (familyMembers.length === 0 && !familyId) {
familyId = 'FAM-' + generateRandomCode();
localStorage.setItem('familyId', familyId);
inviteCode = generateRandomCode();
localStorage.setItem('inviteCode', inviteCode);
// Mark as NOT shared tree (new lineage)
localStorage.setItem('isSharedTree', 'false');
isSharedTree = false;
}
originalOpenFamilyMemberModal(memberId);
};

// Add Relative Functions
function toggleAddRelativeDropdown(memberId, event) {
console.log('toggleAddRelativeDropdown called for member:', memberId);
// Close all other dropdowns first
document.querySelectorAll('.add-relative-dropdown').forEach(dropdown => {
if (dropdown.id !== `add-relative-dropdown-${memberId}`) {
dropdown.style.display = 'none';
dropdown.classList.remove('dropdown-down');
}
});

const dropdown = document.getElementById(`add-relative-dropdown-${memberId}`);
if (dropdown) {
const isHidden = dropdown.style.display === 'none';
dropdown.style.display = isHidden ? 'block' : 'none';
console.log('Dropdown display set to:', isHidden ? 'block' : 'none');

if (isHidden) {
// Check if card is too close to top of container
const card = event.target.closest('.tree-member-card');
const container = document.getElementById('family-tree-container');

if (card && container) {
const cardRect = card.getBoundingClientRect();
const containerRect = container.getBoundingClientRect();
const spaceAbove = cardRect.top - containerRect.top;

// If less than 150px space above, open dropdown downwards
if (spaceAbove < 150) {
dropdown.classList.add('dropdown-down');
} else {
dropdown.classList.remove('dropdown-down');
}
}
}
}
event.stopPropagation();
}

function initiateAddMember(sourceId, relation) {
console.log('initiateAddMember called - Mode: ADD | Source:', sourceId, '| Relationship:', relation);

const form = document.getElementById('family-member-form');
const modal = familyModal || document.getElementById('family-member-modal');
const modalTitle = modal ? modal.querySelector('.modal-title') : null;

if (!form || !modal) {
console.error('Form or modal not found');
return;
}

// NUCLEAR RESET
form.reset();

// Set hidden inputs
document.getElementById('is-editing').value = 'false';
document.getElementById('editing-member-id').value = '';
document.getElementById('family-member-id').value = '';

// Manually clear all fields
document.getElementById('family-member-name').value = '';
document.getElementById('family-member-birth-year').value = '';
document.getElementById('family-member-photo-url').value = '';
document.getElementById('family-member-bio').value = '';
document.getElementById('family-member-linked-account').value = '';
document.getElementById('family-member-alive').value = 'true';

// Clear preview
const preview = document.getElementById('profile-photo-preview');
if (preview) preview.style.display = 'none';
const previewImg = document.getElementById('profile-photo-preview-img');
if (previewImg) previewImg.src = '';

// Hide related fields
const siblingField = document.getElementById('sibling-related-field');
if (siblingField) siblingField.style.display = 'none';
const spouseField = document.getElementById('spouse-related-field');
if (spouseField) spouseField.style.display = 'none';
const accountLinkingField = document.getElementById('account-linking-group');
if (accountLinkingField) accountLinkingField.style.display = 'none';

// Hide delete button
const deleteBtn = document.getElementById('delete-member-btn');
if (deleteBtn) deleteBtn.style.display = 'none';

// Populate parent dropdown
const parentSelect = document.getElementById('family-member-parent');
if (parentSelect) {
parentSelect.innerHTML = '<option value="">No parent (root of tree)</option>';
familyMembers.forEach(member => {
parentSelect.innerHTML += `<option value="${member.id}">${member.name}</option>`;
});
}

// Get source member for title
const sourceMember = familyMembers.find(m => m.id === sourceId);
const sourceName = sourceMember ? sourceMember.name : 'Family Member';

// PRE-FILL ONLY: Relationship and Parent
const relationshipSelect = document.getElementById('family-member-relationship');
if (relationshipSelect) {
relationshipSelect.value = relation;
}

if (parentSelect && sourceId) {
parentSelect.value = sourceId;
}

// Update modal title to show ADD mode
if (modalTitle) {
modalTitle.textContent = `Add ${relation} to ${sourceName}`;
}

// Update button label
const submitBtn = form.querySelector('button[type="submit"]');
if (submitBtn) {
submitBtn.textContent = 'Add to Tree';
}

// Show modal
modal.classList.add('active');
console.log('Modal opened in ADD mode');
}

function addRelative(memberId, relationship) {
console.log('addRelative called with memberId:', memberId, 'relationship:', relationship);
// Close dropdown first
const dropdown = document.getElementById(`add-relative-dropdown-${memberId}`);
if (dropdown) {
dropdown.style.display = 'none';
dropdown.classList.remove('dropdown-down');
}
// Call the new initiateAddMember function
initiateAddMember(memberId, relationship);
}

// Private Stories Functions
function loadPrivateStories() {
renderPrivateStories();
}

function renderPrivateStories() {
const container = document.getElementById('private-stories-container');

if (privateStories.length === 0) {
container.innerHTML = `
<div style="text-align: center; color: var(--text-secondary); padding: 40px;">
<h3 style="margin-bottom: 20px;">No Private Stories Yet</h3>
<p style="margin-bottom: 20px;">Start writing your personal stories with selective access control.</p>
<button class="btn" onclick="openStoryModal()">
<span>+</span> Write First Story
</button>
</div>
`;
return;
}

let html = '';

if (storiesAccessView) {
// Show my stories
html = '<div style="margin-bottom: 20px;"><h4 style="color: var(--text-primary);">My Stories</h4></div>';
privateStories.forEach(story => {
html += renderStoryCard(story);
});
} else {
// Show accessible stories
html = '<div style="margin-bottom: 20px;"><h4 style="color: var(--text-primary);">Accessible Stories</h4></div>';
const accessibleStories = privateStories.filter(story => isStoryAccessible(story));
if (accessibleStories.length === 0) {
html += '<div style="text-align: center; color: var(--text-secondary); padding: 20px;">No accessible stories available.</div>';
} else {
accessibleStories.forEach(story => {
html += renderStoryCard(story);
});
}
}

container.innerHTML = html;
}

function renderStoryCard(story) {
let excerpt = '';
let formatIcon = '';

if (story.format === 'audio') {
excerpt = 'Audio story - Click to play';
formatIcon = 'Audio';
} else if (story.format === 'video') {
excerpt = 'Video story - Click to watch';
formatIcon = 'Video';
} else {
excerpt = story.content.length > 150 ? story.content.substring(0, 150) + '...' : story.content;
formatIcon = 'Text';
}

const accessIcon = story.isPrivate ? 'Private' : (story.familyAccess ? 'Family' : 'Public');
const accessColor = story.isPrivate ? '#ef4444' : (story.familyAccess ? '#3b82f6' : '#10b981');
const tagsHtml = story.tags ? story.tags.split(',').map(tag => 
`<span class="story-tag">${tag.trim()}</span>`
).join('') : '';

return `
<div class="story-card" onclick="viewStory('${story.id}')">
<div class="story-title">${story.title}</div>
<div class="story-excerpt">${excerpt}</div>
<div class="story-meta">
<div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
<span style="background: rgba(74, 158, 255, 0.2); color: var(--accent-color); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem;">${formatIcon}</span>
<span style="color: ${accessColor}; font-size: 0.8rem;">Access: ${accessIcon}</span>
${story.familyAccess && story.allowedFamily ? 
`<span style="color: var(--text-secondary); font-size: 0.8rem;">(${story.allowedFamily.length} family members)</span>` : ''}
</div>
<div class="story-tags">${tagsHtml}</div>
</div>
<div class="story-date">${new Date(story.createdAt).toLocaleDateString()}</div>
</div>
`;
}

function isStoryAccessible(story) {
if (story.isPrivate) return false;
if (!story.familyAccess) return true;
// Check if current user is in allowed family members (simplified)
return story.allowedFamily && story.allowedFamily.length > 0;
}

function openStoryModal(storyId = null) {
const form = document.getElementById('story-form');
const modal = document.getElementById('private-story-modal');

// Populate family member checkboxes
populateFamilyMemberCheckboxes();

if (storyId) {
// Edit existing story
const story = privateStories.find(s => s.id === storyId);
if (story) {
document.getElementById('story-id').value = story.id;
document.getElementById('story-title').value = story.title;
document.getElementById('story-content').value = story.content;
document.getElementById('story-tags').value = story.tags || '';
document.getElementById('story-private').checked = story.isPrivate || false;
document.getElementById('story-family').checked = story.familyAccess || false;

if (story.familyAccess) {
document.getElementById('family-access-group').style.display = 'block';
// Check the allowed family members
story.allowedFamily.forEach(memberId => {
const checkbox = document.getElementById(`family-${memberId}`);
if (checkbox) checkbox.checked = true;
});
}
}
} else {
// Add new story
form.reset();
document.getElementById('story-id').value = '';
}

modal.classList.add('active');
}

function closeStoryModal() {
// Stop any ongoing recordings
if (storyAudioRecorder && storyAudioRecorder.state !== 'inactive') {
stopStoryAudioRecording();
}
if (storyVideoRecorder && storyVideoRecorder.state !== 'inactive') {
stopStoryVideoRecording();
}

// Reset recording variables
storyCurrentAudioBlob = null;
storyCurrentVideoBlob = null;
storyAudioChunks = [];
storyVideoChunks = [];

// Clear previews
document.getElementById('story-audio-preview').innerHTML = '';
document.getElementById('story-recorded-video').innerHTML = '';
document.getElementById('story-video-preview').srcObject = null;

// Reset UI
const audioBtn = document.getElementById('story-record-audio-btn');
audioBtn.innerHTML = '<span>Start Audio Recording</span>';
audioBtn.onclick = startStoryAudioRecording;
audioBtn.style.background = '';

const videoBtn = document.getElementById('story-record-video-btn');
videoBtn.innerHTML = '<span>Start Video Recording</span>';
videoBtn.onclick = startStoryVideoRecording;
videoBtn.style.background = '';

// Close modal and reset form
document.getElementById('private-story-modal').classList.remove('active');
document.getElementById('story-form').reset();

// Show text content group by default
document.getElementById('text-content-group').style.display = 'block';
document.getElementById('audio-content-group').style.display = 'none';
document.getElementById('video-content-group').style.display = 'none';
}

function populateFamilyMemberCheckboxes() {
const container = document.getElementById('family-member-checkboxes');
let html = '';

familyMembers.forEach(member => {
html += `
<label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
<input type="checkbox" id="family-${member.id}" value="${member.id}" style="width: auto;">
<span style="color: var(--text-primary); font-size: 0.9rem;">${member.name}</span>
</label>
`;
});

container.innerHTML = html || '<div style="color: var(--text-secondary);">No family members available.</div>';
}

function toggleStoriesView() {
storiesAccessView = !storiesAccessView;
renderPrivateStories();
}

function viewStory(storyId) {
const story = privateStories.find(s => s.id === storyId);
if (!story) return;

let contentHtml = '';

if (story.format === 'audio' && story.mediaUrl) {
contentHtml = `
<div style="margin-bottom: 20px;">
<audio controls style="width: 100%;">
<source src="${story.mediaUrl}" type="${story.mediaType}">
</audio>
</div>
`;
} else if (story.format === 'video' && story.mediaUrl) {
contentHtml = `
<div style="margin-bottom: 20px;">
<video controls style="width: 100%; max-width: 600px; height: 400px; border-radius: var(--border-radius);">
<source src="${story.mediaUrl}" type="${story.mediaType}">
</video>
</div>
`;
} else {
contentHtml = `
<div style="line-height: 1.8; color: var(--text-primary); white-space: pre-wrap;">${story.content}</div>
`;
}

// Create a simple modal to display the full story
const modal = document.createElement('div');
modal.className = 'modal active';
modal.innerHTML = `
<div class="modal-content" style="max-width: 700px;">
<div class="modal-header">
<h2 class="modal-title">${story.title}</h2>
<button class="close-btn" onclick="this.closest('.modal').remove()">×</button>
</div>
<div style="margin-bottom: 20px;">
<div style="color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 15px;">
${new Date(story.createdAt).toLocaleDateString()} 
${story.tags ? `| Tags: ${story.tags}` : ''}
${story.format ? `| Format: ${story.format.charAt(0).toUpperCase() + story.format.slice(1)}` : ''}
</div>
${contentHtml}
</div>
<div style="text-align: center;">
<button class="btn btn-secondary" onclick="this.closest('.modal').remove()">Close</button>
</div>
</div>
`;
document.body.appendChild(modal);
}

function savePrivateStories() {
localStorage.setItem('privateStories', JSON.stringify(privateStories));
}

// Story Recording Functions
function startStoryAudioRecording() {
navigator.mediaDevices.getUserMedia({ audio: true })
.then(stream => {
storyAudioRecorder = new MediaRecorder(stream);
storyAudioChunks = [];
storyRecordingStartTime = Date.now();

storyAudioRecorder.ondataavailable = event => {
storyAudioChunks.push(event.data);
};

storyAudioRecorder.onstop = () => {
const audioBlob = new Blob(storyAudioChunks, { type: 'audio/webm' });
storyCurrentAudioBlob = audioBlob;
displayStoryAudioPreview(audioBlob);
stopStoryAudioRecording();
};

storyAudioRecorder.start();
updateStoryAudioUI();
startStoryAudioTimer();
startStoryWaveformVisualizer(stream);
})
.catch(err => {
console.error('Error accessing microphone:', err);
showError('Could not access microphone. Please check permissions.');
});
}

function stopStoryAudioRecording() {
if (storyAudioRecorder && storyAudioRecorder.state !== 'inactive') {
storyAudioRecorder.stop();
storyAudioRecorder.stream.getTracks().forEach(track => track.stop());
}
stopStoryAudioTimer();
stopNoSoundDetection();

const btn = document.getElementById('story-record-audio-btn');
const controls = document.getElementById('story-audio-controls');
const largeMicIcon = document.getElementById('recording-mic-icon-large');
const videoBtn = document.getElementById('story-record-video-btn');
const banner = document.getElementById('audio-recording-banner');

// Fade out large mic icon
largeMicIcon.classList.add('fading');

// Wait for fade-out transition, then hide
setTimeout(() => {
largeMicIcon.classList.remove('active', 'fading');
largeMicIcon.style.transform = 'scale(1)';
largeMicIcon.style.filter = 'none';
}, 300);

// Remove recording class from container
controls.classList.remove('is-recording');

// Remove button styling
btn.classList.remove('is-recording-btn');

// Hide small mic icon
document.getElementById('story-recording-mic').style.display = 'none';

// Re-enable video button
videoBtn.disabled = false;
videoBtn.style.opacity = '1';
videoBtn.style.cursor = 'pointer';

// Hide waveform canvas
document.getElementById('story-waveform-canvas').style.display = 'none';

// Reset volume meter
document.getElementById('volume-meter-bar').style.width = '0%';

// Reset banner
banner.textContent = '🎙️ Audio recording started... speak into your microphone.';
banner.classList.remove('warning');
banner.classList.remove('active');
}

function updateStoryAudioUI() {
const btn = document.getElementById('story-record-audio-btn');
const controls = document.getElementById('story-audio-controls');
const largeMicIcon = document.getElementById('recording-mic-icon-large');
const videoBtn = document.getElementById('story-record-video-btn');
const banner = document.getElementById('audio-recording-banner');

btn.innerHTML = '<span>Stop Audio Recording</span>';
btn.onclick = stopStoryAudioRecording;
btn.classList.add('is-recording-btn');

// Add recording class to container
controls.classList.add('is-recording');

// Show large mic icon
largeMicIcon.classList.add('active');

// Reset banner text
banner.textContent = '🎙️ Audio recording started... speak into your microphone.';
banner.classList.remove('warning');
banner.classList.add('active');

// Hide small mic icon
document.getElementById('story-recording-mic').style.display = 'none';

// Disable video button
videoBtn.disabled = true;
videoBtn.style.opacity = '0.5';
videoBtn.style.cursor = 'not-allowed';

// Show waveform canvas
document.getElementById('story-waveform-canvas').style.display = 'block';

// Start no-sound detection timer
startNoSoundDetection();
}

function startStoryAudioTimer() {
const timer = document.getElementById('story-audio-timer');
timer.style.display = 'inline-block';

storyAudioTimerInterval = setInterval(() => {
const elapsed = Math.floor((Date.now() - storyRecordingStartTime) / 1000);
const minutes = Math.floor(elapsed / 60);
const seconds = elapsed % 60;
timer.textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}, 1000);
}

function stopStoryAudioTimer() {
if (storyAudioTimerInterval) {
clearInterval(storyAudioTimerInterval);
document.getElementById('story-audio-timer').style.display = 'none';
}
}

let noSoundDetectionInterval = null;
let lastSoundTime = Date.now();

function startNoSoundDetection() {
lastSoundTime = Date.now();
noSoundDetectionInterval = setInterval(() => {
const timeSinceLastSound = Date.now() - lastSoundTime;
if (timeSinceLastSound > 5000) {
const banner = document.getElementById('audio-recording-banner');
banner.textContent = '⚠️ No sound detected. Check your microphone settings.';
banner.classList.add('warning');
}
}, 1000);
}

function stopNoSoundDetection() {
if (noSoundDetectionInterval) {
clearInterval(noSoundDetectionInterval);
noSoundDetectionInterval = null;
}
}

function updateLastSoundTime() {
lastSoundTime = Date.now();
const banner = document.getElementById('audio-recording-banner');
if (banner.classList.contains('warning')) {
banner.textContent = '🎙️ Audio recording started... speak into your microphone.';
banner.classList.remove('warning');
}
}

function startStoryWaveformVisualizer(stream) {
const canvas = document.getElementById('story-waveform-canvas');
const canvasCtx = canvas.getContext('2d');
const audioContext = new (window.AudioContext || window.webkitAudioContext)();
const analyser = audioContext.createAnalyser();
const source = audioContext.createMediaStreamSource(stream);

source.connect(analyser);
analyser.fftSize = 256;
const bufferLength = analyser.frequencyBinCount;
const dataArray = new Uint8Array(bufferLength);

canvas.width = canvas.offsetWidth;
canvas.height = canvas.offsetHeight;

function draw() {
requestAnimationFrame(draw);
analyser.getByteFrequencyData(dataArray);

// Calculate average volume for volume meter
let sum = 0;
for (let i = 0; i < bufferLength; i++) {
sum += dataArray[i];
}
const average = sum / bufferLength;
const volumePercent = Math.min((average / 255) * 100, 100);
document.getElementById('volume-meter-bar').style.width = volumePercent + '%';

// Update last sound time if volume is above threshold
if (volumePercent > 5) {
updateLastSoundTime();
}

// Apply voice-driven vibration to large mic icon
const largeMicIcon = document.getElementById('recording-mic-icon-large');
if (largeMicIcon && largeMicIcon.classList.contains('active')) {
const scale = 1.0 + (volumePercent / 100) * 0.1; // Scale from 1.0 to 1.1 based on volume
const glowIntensity = volumePercent / 100; // 0 to 1 based on volume
largeMicIcon.style.transform = `scale(${scale})`;
largeMicIcon.style.filter = `drop-shadow(0 0 ${10 + glowIntensity * 20}px rgba(249, 115, 22, ${0.5 + glowIntensity * 0.5}))`;
}

canvasCtx.fillStyle = 'rgba(255, 255, 255, 0.05)';
canvasCtx.fillRect(0, 0, canvas.width, canvas.height);

const barWidth = (canvas.width / bufferLength) * 2.5;
let barHeight;
let x = 0;

for (let i = 0; i < bufferLength; i++) {
barHeight = dataArray[i] / 2;

const gradient = canvasCtx.createLinearGradient(0, canvas.height, 0, canvas.height - barHeight);
gradient.addColorStop(0, '#ef4444');
gradient.addColorStop(1, '#f97316');

canvasCtx.fillStyle = gradient;
canvasCtx.fillRect(x, canvas.height - barHeight, barWidth, barHeight);

x += barWidth + 1;
}
}

draw();
}

function displayStoryAudioPreview(audioBlob) {
const audioUrl = URL.createObjectURL(audioBlob);
const preview = document.getElementById('story-audio-preview');
preview.innerHTML = `
<audio controls style="width: 100%; margin-top: 10px;">
<source src="${audioUrl}" type="audio/webm">
</audio>
<div style="margin-top: 10px;">
<button class="btn btn-secondary" onclick="reRecordStoryAudio()">Re-record Audio</button>
</div>
`;
}

function reRecordStoryAudio() {
storyCurrentAudioBlob = null;
document.getElementById('story-audio-preview').innerHTML = '';
const btn = document.getElementById('story-record-audio-btn');
btn.innerHTML = '<span>Start Audio Recording</span>';
btn.onclick = startStoryAudioRecording;
btn.style.background = '';
}

function startStoryVideoRecording() {
navigator.mediaDevices.getUserMedia({ video: true, audio: true })
.then(stream => {
const videoPreview = document.getElementById('story-video-preview');
videoPreview.srcObject = stream;
videoPreview.play();

storyVideoRecorder = new MediaRecorder(stream);
storyVideoChunks = [];
storyRecordingStartTime = Date.now();

storyVideoRecorder.ondataavailable = event => {
storyVideoChunks.push(event.data);
};

storyVideoRecorder.onstop = () => {
const videoBlob = new Blob(storyVideoChunks, { type: 'video/webm' });
storyCurrentVideoBlob = videoBlob;
displayStoryVideoPreview(videoBlob);
stopStoryVideoRecording();
};

storyVideoRecorder.start();
updateStoryVideoUI();
startStoryVideoTimer();
})
.catch(err => {
console.error('Error accessing camera:', err);
showError('Could not access camera. Please check permissions.');
});
}

function stopStoryVideoRecording() {
if (storyVideoRecorder && storyVideoRecorder.state !== 'inactive') {
storyVideoRecorder.stop();
storyVideoRecorder.stream.getTracks().forEach(track => track.stop());
}
stopStoryVideoTimer();
}

function updateStoryVideoUI() {
const btn = document.getElementById('story-record-video-btn');
btn.innerHTML = '<span>Stop Video Recording</span>';
btn.onclick = stopStoryVideoRecording;
btn.style.background = '#ef4444';
}

function startStoryVideoTimer() {
const timer = document.getElementById('story-video-timer');
timer.style.display = 'block';

storyVideoTimerInterval = setInterval(() => {
const elapsed = Math.floor((Date.now() - storyRecordingStartTime) / 1000);
const minutes = Math.floor(elapsed / 60);
const seconds = elapsed % 60;
timer.textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}, 1000);
}

function stopStoryVideoTimer() {
if (storyVideoTimerInterval) {
clearInterval(storyVideoTimerInterval);
document.getElementById('story-video-timer').style.display = 'none';
}
}

function displayStoryVideoPreview(videoBlob) {
const videoUrl = URL.createObjectURL(videoBlob);
const preview = document.getElementById('story-recorded-video');
preview.innerHTML = `
<video controls style="width: 100%; max-width: 400px; height: 300px; border-radius: var(--border-radius); margin-top: 10px;">
<source src="${videoUrl}" type="video/webm">
</video>
<div style="margin-top: 10px;">
<button class="btn btn-secondary" onclick="reRecordStoryVideo()">Re-record Video</button>
</div>
`;
}

function reRecordStoryVideo() {
storyCurrentVideoBlob = null;
document.getElementById('story-recorded-video').innerHTML = '';
const videoPreview = document.getElementById('story-video-preview');
videoPreview.srcObject = null;

const btn = document.getElementById('story-record-video-btn');
btn.innerHTML = '<span>Start Video Recording</span>';
btn.onclick = startStoryVideoRecording;
btn.style.background = '';
}

// Time Capsule Recording Functions
function startCapsuleAudioRecording() {
navigator.mediaDevices.getUserMedia({ audio: true })
.then(stream => {
capsuleAudioRecorder = new MediaRecorder(stream);
capsuleAudioChunks = [];
capsuleRecordingStartTime = Date.now();

capsuleAudioRecorder.ondataavailable = event => {
capsuleAudioChunks.push(event.data);
};

capsuleAudioRecorder.onstop = () => {
const audioBlob = new Blob(capsuleAudioChunks, { type: 'audio/webm' });
capsuleCurrentAudioBlob = audioBlob;
displayCapsuleAudioPreview(audioBlob);
stopCapsuleAudioRecording();
};

capsuleAudioRecorder.start();
updateCapsuleAudioUI();
startCapsuleAudioTimer();
})
.catch(err => {
console.error('Error accessing microphone:', err);
showError('Could not access microphone. Please check permissions.');
});
}

function stopCapsuleAudioRecording() {
if (capsuleAudioRecorder && capsuleAudioRecorder.state !== 'inactive') {
capsuleAudioRecorder.stop();
capsuleAudioRecorder.stream.getTracks().forEach(track => track.stop());
}
stopCapsuleAudioTimer();

// Hide the stop button
const btn = document.getElementById('capsule-record-audio-btn');
if (btn) {
btn.style.display = 'none';
}
}

function updateCapsuleAudioUI() {
const btn = document.getElementById('capsule-record-audio-btn');
btn.innerHTML = '<span>Stop Audio Recording</span>';
btn.removeEventListener('click', startCapsuleAudioRecording);
btn.addEventListener('click', stopCapsuleAudioRecording);
btn.style.background = '#ef4444';
}

function startCapsuleAudioTimer() {
const timer = document.getElementById('capsule-audio-timer');
timer.style.display = 'block';

capsuleAudioTimerInterval = setInterval(() => {
const elapsed = Math.floor((Date.now() - capsuleRecordingStartTime) / 1000);
const minutes = Math.floor(elapsed / 60);
const seconds = elapsed % 60;
timer.textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}, 1000);
}

function stopCapsuleAudioTimer() {
if (capsuleAudioTimerInterval) {
clearInterval(capsuleAudioTimerInterval);
document.getElementById('capsule-audio-timer').style.display = 'none';
}
}

function displayCapsuleAudioPreview(audioBlob) {
const audioUrl = URL.createObjectURL(audioBlob);
const preview = document.getElementById('capsule-audio-preview');
preview.innerHTML = `
<audio controls style="width: 100%; margin-top: 10px;">
<source src="${audioUrl}" type="audio/webm">
</audio>
<div style="margin-top: 10px;">
<button class="btn btn-secondary" onclick="reRecordCapsuleAudio()">Re-record Audio</button>
</div>
`;
}

function reRecordCapsuleAudio() {
capsuleCurrentAudioBlob = null;
document.getElementById('capsule-audio-preview').innerHTML = '';
const btn = document.getElementById('capsule-record-audio-btn');
btn.innerHTML = '<span>Start Audio Recording</span>';
btn.removeEventListener('click', stopCapsuleAudioRecording);
btn.addEventListener('click', startCapsuleAudioRecording);
btn.style.background = '';
}

function startCapsuleVideoRecording() {
navigator.mediaDevices.getUserMedia({ video: true, audio: true })
.then(stream => {
const videoPreview = document.getElementById('capsule-video-preview');
videoPreview.srcObject = stream;
videoPreview.play();

capsuleVideoRecorder = new MediaRecorder(stream);
capsuleVideoChunks = [];
capsuleRecordingStartTime = Date.now();

capsuleVideoRecorder.ondataavailable = event => {
console.log('Video chunk received, size:', event.data.size);
capsuleVideoChunks.push(event.data);
};

capsuleVideoRecorder.onstop = () => {
console.log('Video recording stopped, chunks collected:', capsuleVideoChunks.length);
const videoBlob = new Blob(capsuleVideoChunks, { type: 'video/webm' });
console.log('Video blob created, size:', videoBlob.size);
capsuleCurrentVideoBlob = videoBlob;
displayCapsuleVideoPreview(videoBlob);
stopCapsuleVideoRecording();
};

capsuleVideoRecorder.start();
updateCapsuleVideoUI();
startCapsuleVideoTimer();
})
.catch(err => {
console.error('Error accessing camera:', err);
showError('Could not access camera. Please check permissions.');
});
}

function stopCapsuleVideoRecording() {
if (capsuleVideoRecorder && capsuleVideoRecorder.state !== 'inactive') {
capsuleVideoRecorder.stop();
capsuleVideoRecorder.stream.getTracks().forEach(track => track.stop());
}
stopCapsuleVideoTimer();

// Hide the stop button
const btn = document.getElementById('capsule-record-video-btn');
if (btn) {
btn.style.display = 'none';
}
}

function updateCapsuleVideoUI() {
const btn = document.getElementById('capsule-record-video-btn');
btn.innerHTML = '<span>Stop Video Recording</span>';
btn.removeEventListener('click', startCapsuleVideoRecording);
btn.addEventListener('click', stopCapsuleVideoRecording);
btn.style.background = '#ef4444';
}

function startCapsuleVideoTimer() {
const timer = document.getElementById('capsule-video-timer');
timer.style.display = 'block';

capsuleVideoTimerInterval = setInterval(() => {
const elapsed = Math.floor((Date.now() - capsuleRecordingStartTime) / 1000);
const minutes = Math.floor(elapsed / 60);
const seconds = elapsed % 60;
timer.textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}, 1000);
}

function stopCapsuleVideoTimer() {
if (capsuleVideoTimerInterval) {
clearInterval(capsuleVideoTimerInterval);
document.getElementById('capsule-video-timer').style.display = 'none';
}
}

function displayCapsuleVideoPreview(videoBlob) {
const videoUrl = URL.createObjectURL(videoBlob);
const preview = document.getElementById('capsule-recorded-video');
preview.innerHTML = `
<video controls style="width: 100%; max-width: 400px; height: 300px; border-radius: var(--border-radius); margin-top: 10px;">
<source src="${videoUrl}" type="video/webm">
</video>
<div style="margin-top: 10px;">
<button class="btn btn-secondary" onclick="reRecordCapsuleVideo()">Re-record Video</button>
</div>
`;
}

function reRecordCapsuleVideo() {
capsuleCurrentVideoBlob = null;
document.getElementById('capsule-recorded-video').innerHTML = '';
const videoPreview = document.getElementById('capsule-video-preview');
videoPreview.srcObject = null;

const btn = document.getElementById('capsule-record-video-btn');
btn.innerHTML = '<span>Start Video Recording</span>';
btn.removeEventListener('click', stopCapsuleVideoRecording);
btn.addEventListener('click', startCapsuleVideoRecording);
btn.style.background = '';
}

// Story format switching
document.addEventListener('DOMContentLoaded', function() {
// Add event listeners for story format radio buttons
document.querySelectorAll('input[name="story-format"]').forEach(radio => {
radio.addEventListener('change', function() {
const format = this.value;

// Hide all content groups
document.getElementById('text-content-group').style.display = 'none';
document.getElementById('audio-content-group').style.display = 'none';
document.getElementById('video-content-group').style.display = 'none';

// Show selected content group
if (format === 'text') {
document.getElementById('text-content-group').style.display = 'block';
} else if (format === 'audio') {
document.getElementById('audio-content-group').style.display = 'block';
} else if (format === 'video') {
document.getElementById('video-content-group').style.display = 'block';
}
});
});

});

function loadFamilyTree() {
renderFamilyTree();
}

// Family access checkbox toggle
const storyFamily = document.getElementById('story-family');
if (storyFamily) {
storyFamily.addEventListener('change', function() {
const familyAccessGroup = document.getElementById('family-access-group');
familyAccessGroup.style.display = this.checked ? 'block' : 'none';
});
}

// Profile photo preview function
function previewProfilePhoto(input) {
if (input.files && input.files[0]) {
const reader = new FileReader();
reader.onload = function(e) {
const preview = document.getElementById('profile-photo-preview');
const previewImg = document.getElementById('profile-photo-preview-img');
preview.style.display = 'block';
previewImg.src = e.target.result;
}
reader.readAsDataURL(input.files[0]);
}
}

// Toggle relationship fields based on relationship selection
function toggleRelationshipFields() {
const relationship = document.getElementById('family-member-relationship').value;
const siblingField = document.getElementById('sibling-related-field');
const spouseField = document.getElementById('spouse-related-field');

if (relationship === 'sibling') {
siblingField.style.display = 'block';
populateSiblingOptions();
spouseField.style.display = 'none';
document.getElementById('family-member-spouse-of').value = '';
} else if (relationship === 'spouse') {
spouseField.style.display = 'block';
populateSpouseOptions();
siblingField.style.display = 'none';
document.getElementById('family-member-sibling-of').value = '';
} else {
siblingField.style.display = 'none';
spouseField.style.display = 'none';
document.getElementById('family-member-sibling-of').value = '';
document.getElementById('family-member-spouse-of').value = '';
}
}

// Populate sibling options dropdown
function populateSiblingOptions() {
const siblingSelect = document.getElementById('family-member-sibling-of');
const currentMemberId = document.getElementById('family-member-id').value;

// Clear existing options except the first one
siblingSelect.innerHTML = '<option value="">Select family member...</option>';

// Add all family members as potential siblings
familyMembers.forEach(member => {
if (member.id !== currentMemberId) {
const option = document.createElement('option');
option.value = member.id;
option.textContent = member.name;
siblingSelect.appendChild(option);
}
});
}

// Populate spouse options dropdown
function populateSpouseOptions() {
const spouseSelect = document.getElementById('family-member-spouse-of');
const currentMemberId = document.getElementById('family-member-id').value;

// Clear existing options except the first one
spouseSelect.innerHTML = '<option value="">Select family member...</option>';

// Add all family members as potential spouses
familyMembers.forEach(member => {
if (member.id !== currentMemberId) {
const option = document.createElement('option');
option.value = member.id;
option.textContent = member.name;
spouseSelect.appendChild(option);
}
});
}

// User Profile Functions
let userProfile = {
name: 'User',
email: 'user@example.com',
photo: null,
bio: '',
theme: 'dark'
};

function openProfileModal() {
const modal = document.getElementById('profile-modal');

// Load current profile data
document.getElementById('user-name').value = userProfile.name;
document.getElementById('user-email-input').value = userProfile.email;
document.getElementById('user-bio').value = userProfile.bio;
document.getElementById('user-theme').value = userProfile.theme;
document.getElementById('user-photo-url').value = userProfile.photo || '';

// Show profile picture if exists
if (userProfile.photo) {
const preview = document.getElementById('user-photo-preview');
const previewImg = document.getElementById('user-photo-preview-img');
preview.style.display = 'block';
previewImg.src = userProfile.photo;
} else {
document.getElementById('user-photo-preview').style.display = 'none';
}

modal.classList.add('active');
}

function closeProfileModal() {
document.getElementById('profile-modal').classList.remove('active');
document.getElementById('profile-form').reset();

// Reset profile photo preview
document.getElementById('user-photo-preview').style.display = 'none';
document.getElementById('user-photo-preview-img').src = '';
}

function previewUserPhoto(input) {
if (input.files && input.files[0]) {
const reader = new FileReader();
reader.onload = function(e) {
const preview = document.getElementById('user-photo-preview');
const previewImg = document.getElementById('user-photo-preview-img');
preview.style.display = 'block';
previewImg.src = e.target.result;
}
reader.readAsDataURL(input.files[0]);
}
}

// Profile Form Submission
const profileForm = document.getElementById('profile-form');
if (profileForm) {
profileForm.addEventListener('submit', function(e) {
e.preventDefault();

const name = document.getElementById('user-name').value;
const email = document.getElementById('user-email-input').value;
const photoUrl = document.getElementById('user-photo-url').value;
const bio = document.getElementById('user-bio').value;
const theme = document.getElementById('user-theme').value;

// Handle profile picture file upload
const photoFile = document.getElementById('user-photo-file');
let photoData = photoUrl || null;

if (photoFile.files && photoFile.files[0]) {
const reader = new FileReader();
reader.onload = function(e) {
photoData = e.target.result;
saveUserProfile(name, email, photoData, bio, theme);
};
reader.readAsDataURL(photoFile.files[0]);
} else {
saveUserProfile(name, email, photoData, bio, theme);
}
});
}

function saveUserProfile(name, email, photoData, bio, theme) {
userProfile.name = name;
userProfile.email = email;
userProfile.photo = photoData;
userProfile.bio = bio;
userProfile.theme = theme;

// Save to localStorage
localStorage.setItem('userProfile', JSON.stringify(userProfile));

// Update UI
updateUserDisplay();
closeProfileModal();
showSuccess('Profile updated successfully!');
}

function updateUserDisplay() {
// Update email display
document.getElementById('user-email').textContent = userProfile.email;

// Update avatar
const avatarDisplay = document.getElementById('user-avatar-display');
if (userProfile.photo) {
avatarDisplay.style.backgroundImage = `url(${userProfile.photo})`;
avatarDisplay.style.backgroundSize = 'cover';
avatarDisplay.style.backgroundPosition = 'center';
avatarDisplay.textContent = '';
} else {
avatarDisplay.style.backgroundImage = '';
avatarDisplay.textContent = userProfile.name.charAt(0).toUpperCase();
}
}

function loadUserProfile() {
const savedProfile = localStorage.getItem('userProfile');
if (savedProfile) {
userProfile = JSON.parse(savedProfile);
updateUserDisplay();
}
}

// Load user profile on page load
loadUserProfile();

// Update user email display from localStorage
const userEmail = localStorage.getItem('userEmail');
if (userEmail) {
document.getElementById('user-email').textContent = userEmail;
}

// Update avatar with first letter of username
const userName = localStorage.getItem('userName');
const avatarDisplay = document.getElementById('user-avatar-display');
if (userName) {
avatarDisplay.textContent = userName.charAt(0).toUpperCase();
}

// Check authentication
function checkAuthentication() {
if (!localStorage.getItem('userEmail')) {
window.location.href = '/';
}
}

// Check authentication on page load
checkAuthentication();

// Handle hash navigation for section scrolling
function handleHashNavigation() {
const hash = window.location.hash;
if (hash) {
const element = document.querySelector(hash);
if (element) {
element.scrollIntoView({ behavior: 'smooth' });
}
}
}

// Handle hash navigation on page load and hash change
window.addEventListener('load', handleHashNavigation);
window.addEventListener('hashchange', handleHashNavigation);

// Function to show notifications (placeholder)
function showNotifications() {
alert('Notifications feature coming soon!');
}

// Function to share memory with user
async function shareMemory(memoryId, shareWithEmail) {
if (!shareWithEmail) {
alert('Please enter an email address to share with');
return;
}

try {
// For now, just show success message since we don't have backend integration
alert(`Memory shared with ${shareWithEmail}`);
} catch (error) {
console.error('Error sharing memory:', error);
alert('Failed to share memory');
}
}

// Private Story Form Submission
const storyForm = document.getElementById('story-form');
if (storyForm) {
storyForm.addEventListener('submit', function(e) {
e.preventDefault();

const storyId = document.getElementById('story-id').value;
const title = document.getElementById('story-title').value;
const tags = document.getElementById('story-tags').value;
const isPrivate = document.getElementById('story-private').checked;
const familyAccess = document.getElementById('story-family').checked;

// Get story format and content
const format = document.querySelector('input[name="story-format"]:checked').value;
let content = '';
let mediaBlob = null;
let mediaType = null;

if (format === 'text') {
content = document.getElementById('story-content').value;
if (!content.trim()) {
showError('Please write your story content.');
return;
}
} else if (format === 'audio') {
if (!storyCurrentAudioBlob) {
showError('Please record your audio story.');
return;
}
mediaBlob = storyCurrentAudioBlob;
mediaType = 'audio/webm';
content = 'Audio story recorded';
} else if (format === 'video') {
if (!storyCurrentVideoBlob) {
showError('Please record your video story.');
return;
}
mediaBlob = storyCurrentVideoBlob;
mediaType = 'video/webm';
content = 'Video story recorded';
}

// Get selected family members
const allowedFamily = [];
if (familyAccess) {
const checkboxes = document.querySelectorAll('#family-member-checkboxes input[type="checkbox"]:checked');
checkboxes.forEach(checkbox => {
allowedFamily.push(checkbox.value);
});
}

if (storyId) {
// Update existing story
const story = privateStories.find(s => s.id === storyId);
if (story) {
story.title = title;
story.content = content;
story.tags = tags;
story.isPrivate = isPrivate;
story.familyAccess = familyAccess;
story.allowedFamily = allowedFamily;
story.format = format;
story.mediaType = mediaType;
if (mediaBlob) {
story.mediaUrl = URL.createObjectURL(mediaBlob);
}
}
} else {
// Add new story
const newStory = {
id: generateId(),
title: title,
content: content,
tags: tags,
isPrivate: isPrivate,
familyAccess: familyAccess,
allowedFamily: allowedFamily,
format: format,
mediaType: mediaType,
createdAt: new Date().toISOString()
};

if (mediaBlob) {
newStory.mediaUrl = URL.createObjectURL(mediaBlob);
}

privateStories.push(newStory);
}

savePrivateStories();
closeStoryModal();
renderPrivateStories();
showSuccess(storyId ? 'Story updated successfully!' : 'Story created successfully!');
});
}

// Tab switching
function switchTab(tabName) {
// Hide all tab contents
document.querySelectorAll('.tab-content').forEach(tab => {
tab.classList.remove('active');
});

// Remove active class from all nav buttons
document.querySelectorAll('.nav-btn').forEach(btn => {
btn.classList.remove('active');
});

// Show selected tab content
document.getElementById(`tab-${tabName}`).classList.add('active');

// Add active class to selected nav button
document.querySelector(`.nav-btn[data-tab="${tabName}"]`).classList.add('active');

// Hide/Show Storytelling Engine based on active tab
const storytellingSection = document.querySelector('.storytelling-section');
if (storytellingSection) {
if (tabName === 'roots') {
storytellingSection.style.display = 'none';
} else {
storytellingSection.style.display = 'block';
}
}

// Load data for the selected tab
if (tabName === 'inbox') {
loadIncomingMemories();
} else if (tabName === 'roots') {
loadFamilyTree();
}
}

// Load incoming memories (shared + family)
async function loadIncomingMemories() {
const incomingContainer = document.getElementById('incoming-memories');

try {
console.log('loadIncomingMemories: Fetching shared memories...');
const response = await fetch('/api/memories/shared', {
credentials: 'include'
});
if (!response.ok) {
throw new Error('Failed to load incoming memories');
}
const sharedMemories = await response.json();
console.log('loadIncomingMemories: Shared memories received:', sharedMemories);

// Add shared memories to global memories array
sharedMemories.forEach(m => {
if (!memories.find(existing => existing.id === m.id)) {
memories.push(m);
}
});

if (sharedMemories.length === 0) {
incomingContainer.innerHTML = '<div class="memory-card"><div class="memory-icon">📥</div><h3>No Incoming Memories</h3><p>No memories here yet. Start your legacy in the Create tab!</p></div>';
return;
}

// Store full list for toggle
incomingContainer.dataset.fullItems = JSON.stringify(sharedMemories);

// Render limited view (last 5)
const limitedMemories = sharedMemories.slice(-5);
renderIncomingMemoriesList(incomingContainer, limitedMemories, sharedMemories.length);

// Add event listeners
incomingContainer.querySelectorAll('.memory-card').forEach(card => {
card.addEventListener('click', function() {
const memoryId = this.dataset.memoryId;
console.log('Capsule clicked:', memoryId);
viewMemory(memoryId);
});
});
} catch (error) {
console.error('Error loading incoming memories:', error);
incomingContainer.innerHTML = '<div class="memory-card"><div class="memory-icon">📥</div><h3>Error Loading</h3><p>Failed to load incoming memories</p></div>';
}
}

function renderIncomingMemoriesList(container, memories, totalCount) {
// Remove existing show more button
const existingBtn = container.querySelector('.show-more-btn');
if (existingBtn) existingBtn.remove();

// Remove loading message if present
const loading = container.querySelector('.loading');
if (loading) loading.remove();

container.innerHTML = memories.map(memory => `
<button class="memory-card released" data-memory-id="${memory.id}" style="width: 100%; text-align: left; cursor: pointer;">
<div class="memory-title">${memory.title}</div>
<div class="memory-date">Shared with you • Created: ${formatDate(memory.created_at)}</div>
${memory.video_url && memory.video_url !== 'None' ? `<video src="${memory.video_url}" controls playsinline preload="auto" style="width: 100%; max-height: 200px; margin: 10px 0; border-radius: 8px; pointer-events: none;"></video>` : ''}
<div class="memory-status released">📥 Received</div>
<div class="memory-meta">
<div>Release: ${formatDate(memory.release_date)}</div>
<div class="media-indicators">
${memory.has_video ? '<div class="media-indicator">🎥</div>' : ''}
${memory.has_image ? '<div class="media-indicator">🖼️</div>' : ''}
</div>
</div>
</button>
`).join('');

// Add show more/less button if needed
if (totalCount > 5) {
const showMoreBtn = document.createElement('button');
showMoreBtn.className = 'show-more-btn';
const isExpanded = container.parentElement.classList.contains('expanded');
showMoreBtn.textContent = isExpanded ? 'Show Less' : 'Show More';
showMoreBtn.onclick = () => toggleSection('incoming-memories');
container.appendChild(showMoreBtn);
}

// Add event listeners
container.querySelectorAll('.memory-card').forEach(card => {
card.addEventListener('click', function() {
const memoryId = this.dataset.memoryId;
console.log('Capsule clicked:', memoryId);
viewMemory(memoryId);
});
});
}

function toggleSection(sectionId) {
const wrapper = document.getElementById(`${sectionId}-wrapper`);
const container = document.getElementById(sectionId);

if (!wrapper || !container) return;

const isExpanded = wrapper.classList.contains('expanded');
const fullItems = container.dataset.fullItems;

if (isExpanded) {
// Collapse to limited view
wrapper.classList.remove('expanded');
if (fullItems) {
const items = JSON.parse(fullItems);
const limitedItems = items.slice(-5);

if (sectionId === 'active-capsules') {
renderCapsulesList(container, limitedItems, items.length, false);
} else if (sectionId === 'opened-memories') {
renderMemoriesList(container, limitedItems, items.length, false);
} else if (sectionId === 'incoming-memories') {
renderIncomingMemoriesList(container, limitedItems, items.length);
}
}
} else {
// Expand to full view
wrapper.classList.add('expanded');
if (fullItems) {
const items = JSON.parse(fullItems);

if (sectionId === 'active-capsules') {
renderCapsulesList(container, items, items.length, true);
} else if (sectionId === 'opened-memories') {
renderMemoriesList(container, items, items.length, true);
} else if (sectionId === 'incoming-memories') {
renderIncomingMemoriesList(container, items, items.length);
}
}
}
}

// Initialize
document.addEventListener('DOMContentLoaded', function() {
loadMemories();
loadFromLocalStorage();
rotateStoryPrompts();

// Set minimum release date to current local time to prevent selecting past times
// datetime-local input uses browser's local timezone
const now = new Date();
// Get local date and time in the format datetime-local expects (YYYY-MM-DDTHH:mm)
const localISOTime = new Date(now.getTime() - (now.getTimezoneOffset() * 60000)).toISOString().slice(0, 16);
document.getElementById('release-date').min = localISOTime;
});

// Memory Management
async function loadMemories() {
try {
console.log('loadMemories: Fetching from API...');
const response = await fetch('/api/memories', {
credentials: 'include'
});
if (!response.ok) {
throw new Error('Failed to load memories');
}
const fetchedMemories = await response.json();
console.log('loadMemories: Fetched memories:', fetchedMemories);
console.log('loadMemories: First memory video_url:', fetchedMemories[0]?.video_url);

// Update global memories array
memories = fetchedMemories;

// Backend already filters by user_id (creator), so no frontend filter needed
const myCapsules = memories;
const openedMemories = memories.filter(m => m.status === 'released');

renderActiveCapsules(myCapsules);
renderOpenedMemories(openedMemories);

// Start countdown timers
myCapsules.forEach(memory => {
startCountdown(memory);
});
} catch (error) {
console.error('Error loading memories:', error);
const container = document.getElementById('active-capsules');
container.innerHTML = '<p style="text-align: center; color: var(--text-secondary);">Failed to load memories</p>';
}
}

function renderActiveCapsules(capsules) {
const container = document.getElementById('active-capsules');
const wrapper = document.getElementById('active-capsules-wrapper');

if (capsules.length === 0) {
container.innerHTML = '<div class="memory-card"><div class="memory-icon">🔒</div><h3>No Active Capsules</h3><p>No memories here yet. Start your legacy in the Create tab!</p></div>';
return;
}

// Sort by date (newest first)
capsules.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

// Store full list for toggle
container.dataset.fullItems = JSON.stringify(capsules);

// Check if this section is expanded
const isExpanded = wrapper.classList.contains('expanded');

if (isExpanded) {
// Show all items in expanded state
renderCapsulesList(container, capsules, capsules.length, true);
} else {
// Render limited view (last 5)
const limitedCapsules = capsules.slice(-5);
renderCapsulesList(container, limitedCapsules, capsules.length, false);
}
}

function renderCapsulesList(container, capsules, totalCount, isExpanded) {
// Remove existing show more button
const existingBtn = container.querySelector('.show-more-btn');
if (existingBtn) existingBtn.remove();

// Keep only the memory cards, remove any existing content
const existingCards = container.querySelectorAll('.memory-card');
existingCards.forEach(card => card.remove());

// Remove loading message if present
const loading = container.querySelector('.loading');
if (loading) loading.remove();

container.innerHTML = capsules.map(memory => `
<div class="memory-card locked" onclick="viewMemory('${memory.id}')" style="cursor: pointer;">
<div class="memory-title">${memory.title}</div>
<div class="memory-date">Created: ${formatDate(memory.created_at)}</div>
${memory.video_url && memory.video_url !== 'None' ? `<video src="${memory.video_url}" controls playsinline preload="auto" style="width: 100%; max-height: 200px; margin: 10px 0; border-radius: 8px; pointer-events: none;"></video>` : ''}
<div class="countdown" id="countdown-${memory.id}">
Loading countdown...
</div>
<div class="memory-status locked">🔒 Locked</div>
<div class="memory-meta">
<div>Release: ${formatDate(memory.release_date)}</div>
<div class="media-indicators">
${memory.has_video ? '<div class="media-indicator">🎥</div>' : ''}
${memory.has_image ? '<div class="media-indicator">🖼️</div>' : ''}
</div>
</div>
</div>
`).join('');

// Add show less button if expanded
if (isExpanded) {
const showLessBtn = document.createElement('button');
showLessBtn.className = 'show-more-btn show-less';
showLessBtn.textContent = '↑ Show Less';
showLessBtn.onclick = () => toggleSection('active-capsules');
container.appendChild(showLessBtn);
}
// Add show more button if not expanded and has more than 5 items
else if (totalCount > 5) {
const showMoreBtn = document.createElement('button');
showMoreBtn.className = 'show-more-btn';
showMoreBtn.textContent = 'Show More';
showMoreBtn.onclick = () => toggleSection('active-capsules');
container.appendChild(showMoreBtn);
}
}

function renderOpenedMemories(memories) {
const container = document.getElementById('opened-memories');
const wrapper = document.getElementById('opened-memories-wrapper');

if (memories.length === 0) {
container.innerHTML = '<div class="memory-card"><div class="memory-icon">📖</div><h3>No Personal Collection</h3><p>No memories here yet. Start your legacy in the Create tab!</p></div>';
return;
}

// Sort by date (newest first)
memories.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

// Store full list for toggle
container.dataset.fullItems = JSON.stringify(memories);

// Check if this section is expanded
const isExpanded = wrapper.classList.contains('expanded');

if (isExpanded) {
// Show all items in expanded state
renderMemoriesList(container, memories, memories.length, true);
} else {
// Render limited view (last 5)
const limitedMemories = memories.slice(-5);
renderMemoriesList(container, limitedMemories, memories.length, false);
}
}

function renderMemoriesList(container, memories, totalCount, isExpanded) {
// Remove existing show more button
const existingBtn = container.querySelector('.show-more-btn');
if (existingBtn) existingBtn.remove();

// Remove loading message if present
const loading = container.querySelector('.loading');
if (loading) loading.remove();

container.innerHTML = memories.map(memory => `
<div class="memory-card released" onclick="viewMemory('${memory.id}')" style="cursor: pointer;">
<div class="memory-title">${memory.title}</div>
<div class="memory-date">Opened: ${formatDate(memory.release_date)}</div>
${memory.video_url && memory.video_url !== 'None' ? `<video src="${memory.video_url}" controls playsinline preload="auto" style="width: 100%; max-height: 200px; margin: 10px 0; border-radius: 8px; pointer-events: none;"></video>` : ''}
<div class="memory-status released">🔓 Released</div>
<div class="memory-meta">
<div>Created: ${formatDate(memory.created_at)}</div>
<div class="media-indicators">
${memory.has_video ? '<div class="media-indicator">🎥</div>' : ''}
${memory.has_image ? '<div class="media-indicator">🖼️</div>' : ''}
</div>
</div>
</div>
`).join('');

// Add show less button if expanded
if (isExpanded) {
const showLessBtn = document.createElement('button');
showLessBtn.className = 'show-more-btn show-less';
showLessBtn.textContent = '↑ Show Less';
showLessBtn.onclick = () => toggleSection('opened-memories');
container.appendChild(showLessBtn);
}
// Add show more button if not expanded and has more than 5 items
else if (totalCount > 5) {
const showMoreBtn = document.createElement('button');
showMoreBtn.className = 'show-more-btn';
showMoreBtn.textContent = 'Show More';
showMoreBtn.onclick = () => toggleSection('opened-memories');
container.appendChild(showMoreBtn);
}
}

// Countdown Timer
function startCountdown(memory) {
const countdownElement = document.getElementById(`countdown-${memory.id}`);
if (!countdownElement) return;

function updateCountdown() {
const now = new Date().getTime();
const releaseTime = new Date(memory.release_date).getTime();
const distance = releaseTime - now;

if (distance < 0) {
countdownElement.innerHTML = '🔓 Released!';
countdownElement.classList.add('expired');
return;
}

const days = Math.floor(distance / (1000 * 60 * 60 * 24));
const hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
const seconds = Math.floor((distance % (1000 * 60)) / 1000);

countdownElement.innerHTML = `⏰ ${days}d ${hours}h ${minutes}m ${seconds}s`;
}

updateCountdown();
setInterval(updateCountdown, 1000);
}

// Modal Functions
function openCreateModal() {
document.getElementById('create-modal').classList.add('active');

// Add event listeners for recording buttons
setTimeout(() => {
const audioBtn = document.getElementById('capsule-record-audio-btn');
const videoBtn = document.getElementById('capsule-record-video-btn');

if (audioBtn) {
audioBtn.addEventListener('click', startCapsuleAudioRecording);
console.log('Audio button listener added');
}

if (videoBtn) {
videoBtn.addEventListener('click', startCapsuleVideoRecording);
console.log('Video button listener added');
}
}, 200);
}

function handleCapsuleFormatChange(event) {
const format = event.target.value;
console.log('Capsule format changed to:', format);

// Hide all content groups
const textGroup = document.getElementById('text-content-group');
const audioGroup = document.getElementById('audio-content-group');
const videoGroup = document.getElementById('video-content-group');

console.log('Content groups found:', {
textGroup: !!textGroup,
audioGroup: !!audioGroup,
videoGroup: !!videoGroup
});

if (textGroup) textGroup.style.display = 'none';
if (audioGroup) audioGroup.style.display = 'none';
if (videoGroup) videoGroup.style.display = 'none';

// Show selected content group
if (format === 'text') {
if (textGroup) {
textGroup.style.display = 'block';
console.log('Showing text content group');
} else {
console.error('Text group not found!');
}
} else if (format === 'audio') {
if (audioGroup) {
audioGroup.style.display = 'block';
console.log('Showing audio content group');
} else {
console.error('Audio group not found!');
}
} else if (format === 'video') {
if (videoGroup) {
videoGroup.style.display = 'block';
console.log('Showing video content group');
} else {
console.error('Video group not found!');
}
}
}

function switchCapsuleFormat(format) {
console.log('Switching capsule format to:', format);

// Hide all content groups
const textGroup = document.getElementById('capsule-text-content-group');
const audioGroup = document.getElementById('capsule-audio-content-group');
const videoGroup = document.getElementById('capsule-video-content-group');

if (textGroup) textGroup.style.display = 'none';
if (audioGroup) audioGroup.style.display = 'none';
if (videoGroup) videoGroup.style.display = 'none';

// Show selected content group
if (format === 'text') {
if (textGroup) textGroup.style.display = 'block';
} else if (format === 'audio') {
if (audioGroup) audioGroup.style.display = 'block';
} else if (format === 'video') {
if (videoGroup) videoGroup.style.display = 'block';
}
}

function closeCreateModal() {
// Stop any ongoing recordings
if (capsuleAudioRecorder && capsuleAudioRecorder.state !== 'inactive') {
stopCapsuleAudioRecording();
}
if (capsuleVideoRecorder && capsuleVideoRecorder.state !== 'inactive') {
stopCapsuleVideoRecording();
}

// Reset recording variables
capsuleCurrentAudioBlob = null;
capsuleCurrentVideoBlob = null;
capsuleAudioChunks = [];
capsuleVideoChunks = [];

// Clear previews
document.getElementById('capsule-audio-preview').innerHTML = '';
document.getElementById('capsule-recorded-video').innerHTML = '';
document.getElementById('capsule-video-preview').srcObject = null;

// Reset UI
const audioBtn = document.getElementById('capsule-record-audio-btn');
audioBtn.innerHTML = '<span>Start Audio Recording</span>';
audioBtn.removeEventListener('click', stopCapsuleAudioRecording);
audioBtn.removeEventListener('click', startCapsuleAudioRecording);
audioBtn.addEventListener('click', startCapsuleAudioRecording);
audioBtn.style.background = '';

const videoBtn = document.getElementById('capsule-record-video-btn');
videoBtn.innerHTML = '<span>Start Video Recording</span>';
videoBtn.removeEventListener('click', stopCapsuleVideoRecording);
videoBtn.removeEventListener('click', startCapsuleVideoRecording);
videoBtn.addEventListener('click', startCapsuleVideoRecording);
videoBtn.style.background = '';

// Close modal and reset form
document.getElementById('create-modal').classList.remove('active');
document.getElementById('create-form').reset();

// Show text content group by default
document.getElementById('capsule-text-content-group').style.display = 'block';
document.getElementById('capsule-audio-content-group').style.display = 'none';
document.getElementById('capsule-video-content-group').style.display = 'none';
}

async function viewMemory(memoryId) {
console.log('=== VIEW MEMORY DEBUG ===');
console.log('Memory ID clicked:', memoryId);
let memory = memories.find(m => m.id == memoryId);
console.log('Memory found in local array:', memory);

if (!memory) {
console.log('Memory not found locally, fetching from API...');
// Try fetching from API if not found locally
try {
const response = await fetch(`/api/memories/${memoryId}`, {
credentials: 'include'
});
console.log('API response status:', response.status);
if (response.ok) {
memory = await response.json();
console.log('=== FULL API RESPONSE DATA ===');
console.log('Memory object:', JSON.stringify(memory, null, 2));
console.log('video_url:', memory.video_url);
console.log('audio_url:', memory.audio_url);
console.log('has_video:', memory.has_video);
console.log('has_audio:', memory.has_audio);
console.log('image_url:', memory.image_url);
console.log('has_image:', memory.has_image);
memories.push(memory); // Add to local cache
} else {
console.error(`Memory ${memoryId} not found on server, status: ${response.status}`);
return;
}
} catch (error) {
console.error(`Error fetching memory ${memoryId}:`, error);
return;
}
}

if (!memory) {
console.error(`Memory ${memoryId} not found`);
return;
}

const now = new Date();
const releaseTime = new Date(memory.release_date);
console.log('Current time:', now);
console.log('Release time:', releaseTime);
console.log('Is released:', now >= releaseTime);

if (now < releaseTime && memory.status === 'locked') {
console.log('Memory is still locked until release date');
return;
}

console.log('Opening modal for memory:', memory.title);
console.log('Memory object:', memory);
console.log('Video URL:', memory.video_url);
console.log('Audio URL:', memory.audio_url);
console.log('Has video:', memory.has_video);
console.log('Has audio:', memory.has_audio);
document.getElementById('memory-modal-title').textContent = memory.title;

let mediaHtml = '';

// Check for audio first
if (memory.audio_url && memory.audio_url !== 'None') {
mediaHtml = `<audio controls style="width: 100%; max-width: 400px; border-radius: var(--border-radius);"><source src="${memory.audio_url}" type="audio/webm"></audio>`;
}
// Then check for video
else if (memory.video_url && memory.video_url !== 'None') {
mediaHtml = `<video controls playsinline preload="auto" style="width: 100%; max-width: 400px; border-radius: var(--border-radius);"><source src="${memory.video_url}" type="video/webm"></video>`;
}
// No media
else {
mediaHtml = '<p style="text-align: center; color: var(--text-secondary);">No media recorded for this capsule</p>';
}

const contentHtml = `
<div class="memory-date">Created: ${formatDate(memory.created_at)}</div>
<div class="memory-date">Released: ${formatDate(memory.release_date)}</div>
<div style="margin: 20px 0; line-height: 1.8;">
${memory.content ? memory.content.replace(/\n/g, '<br>') : ''}
</div>
${mediaHtml}
${memory.image_url ? `<img src="${memory.image_url}" style="width: 100%; max-width: 400px; border-radius: var(--border-radius);" alt="Memory image">` : ''}
`;

document.getElementById('memory-modal-content').innerHTML = contentHtml;
document.getElementById('memory-modal').classList.add('active');
}

function closeMemoryModal() {
document.getElementById('memory-modal').classList.remove('active');
}

// Audio Recording
async function toggleAudioRecording() {
const btn = document.getElementById('audio-record-btn');

if (!isRecording) {
try {
const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
audioRecorder = new MediaRecorder(stream);
audioChunks = [];

audioRecorder.ondataavailable = (event) => {
audioChunks.push(event.data);
};

audioRecorder.onstop = async () => {
const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
const audioUrl = URL.createObjectURL(audioBlob);
displayRecordedAudio(audioUrl, audioBlob.size);
await uploadStoryAudio(audioBlob);
};

audioRecorder.start();
isRecording = true;
btn.classList.add('recording');
btn.innerHTML = '<span>⏹️</span> Stop Recording';
} catch (error) {
showError('Failed to access microphone: ' + error.message);
}
} else {
if (audioRecorder) {
audioRecorder.stop();
audioRecorder.stream.getTracks().forEach(track => track.stop());
}
isRecording = false;
btn.classList.remove('recording');
btn.innerHTML = '<span>🎤</span> Record Audio Story';
}
}

async function toggleVideoRecording() {
const btn = document.getElementById('video-record-btn');
const videoPreview = document.getElementById('live-video');
const timerElement = document.getElementById('video-timer');

if (!isVideoRecording) {
try {
const stream = await navigator.mediaDevices.getUserMedia({ 
video: true, 
audio: true 
});
currentStream = stream; // Store the stream
videoRecorder = new MediaRecorder(stream);
videoChunks = [];
recordingStartTime = Date.now();

videoRecorder.ondataavailable = (event) => {
videoChunks.push(event.data);
};

videoRecorder.onstop = async () => {
const videoBlob = new Blob(videoChunks, { 
type: 'video/webm' 
});
const duration = (Date.now() - recordingStartTime) / 1000;
const videoUrl = URL.createObjectURL(videoBlob);

const videoData = {
id: generateId(),
blob: videoBlob,
url: videoUrl,
duration: duration,
timestamp: new Date().toISOString(),
size: videoBlob.size,
transmitted: false
};

recordedVideos.push(videoData);
saveToLocalStorage();

// Stop the stream and clear preview
if (currentStream) {
currentStream.getTracks().forEach(track => track.stop());
currentStream = null;
}
videoPreview.srcObject = null;
videoPreview.style.display = 'none';
timerElement.style.display = 'none';

displayRecordedVideo(videoUrl, videoBlob.size, duration);
await uploadVideo(videoBlob, duration);
showTransmissionQueue();
};

videoRecorder.start();
isVideoRecording = true;
btn.classList.add('recording');
btn.innerHTML = '<span>⏹️</span> Stop Recording';

// Show live video preview - ensure it plays
videoPreview.srcObject = stream;
videoPreview.style.display = 'block';

// Wait for the video to be ready before playing
videoPreview.onloadedmetadata = () => {
videoPreview.play().catch(e => {
console.log('Auto-play prevented, trying manual play:', e);
// Try manual play as fallback
setTimeout(() => {
videoPreview.play().catch(err => console.log('Manual play failed:', err));
}, 100);
});
};

// Start recording timer
updateVideoTimer();
timerElement.style.display = 'block';

showSuccess('Video recording started...');
} catch (error) {
showError('Failed to access camera/microphone: ' + error.message);
}
} else {
if (videoRecorder) {
videoRecorder.stop();
}
isVideoRecording = false;
btn.classList.remove('recording');
btn.innerHTML = '<span>🎥</span> Record Video Story';

// Stop the stream and clear preview
if (currentStream) {
currentStream.getTracks().forEach(track => track.stop());
currentStream = null;
}
videoPreview.srcObject = null;
videoPreview.style.display = 'none';
timerElement.style.display = 'none';

// Clear timer
if (videoTimerInterval) {
clearInterval(videoTimerInterval);
}
}
}

function displayRecordedAudio(audioUrl, size) {
// Generate filename immediately
const timestamp = Date.now();
const filename = `audio_${timestamp}.webm`;
window.currentFilename = filename;

console.log('=== AUDIO BLOB VERIFICATION ===');
console.log('Audio URL:', audioUrl);
console.log('Audio size:', size);
console.log('Filename generated:', filename);

// Hide recording interface, show side-by-side layout
document.getElementById('recording-interface').style.display = 'none';
document.getElementById('media-schedule-layout').style.display = 'flex';

// Show audio preview
const mediaPreview = document.getElementById('media-preview-side');
mediaPreview.innerHTML = `
<h4 style="color: var(--success-color); margin-bottom: 10px;">Audio Recorded</h4>
<audio controls style="width: 100%; margin-bottom: 10px;">
<source src="${audioUrl}" type="audio/webm">
</audio>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Size: ${(size / 1024 / 1024).toFixed(2)}MB
</div>
`;

// Store the audio URL for scheduling
window.currentMediaUrl = audioUrl;
window.currentMediaType = 'audio';
}

function displayRecordedVideo(videoUrl, size, duration) {
// Generate filename immediately
const timestamp = Date.now();
const filename = `video_${timestamp}.webm`;
window.currentFilename = filename;

console.log('=== VIDEO BLOB VERIFICATION ===');
console.log('Video URL:', videoUrl);
console.log('Video size:', size);
console.log('Video duration:', duration);
console.log('Filename generated:', filename);

// Hide recording interface, show side-by-side layout
document.getElementById('recording-interface').style.display = 'none';
document.getElementById('media-schedule-layout').style.display = 'flex';

// Show video preview
const mediaPreview = document.getElementById('media-preview-side');
mediaPreview.innerHTML = `
<h4 style="color: var(--success-color); margin-bottom: 10px;">Video Recorded</h4>
<video controls style="width: 100%; max-width: 400px; border-radius: 8px; margin-bottom: 10px;">
<source src="${videoUrl}" type="video/webm">
</video>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Duration: ${Math.floor(duration)}s | Size: ${(size / 1024 / 1024).toFixed(2)}MB
</div>
`;

// Store the video URL for scheduling
window.currentMediaUrl = videoUrl;
window.currentMediaType = 'video';
}

function resetRecording() {
// Reset to recording interface
document.getElementById('recording-interface').style.display = 'block';
document.getElementById('media-schedule-layout').style.display = 'none';
document.getElementById('media-preview-side').innerHTML = '';

// Clear form
document.getElementById('schedule-title').value = '';
document.getElementById('schedule-recipient').value = '';
document.getElementById('schedule-date').value = '';
document.getElementById('schedule-visibility').value = 'private';

// Clear stored media
window.currentMediaUrl = null;
window.currentMediaType = null;
window.currentFilename = null;
}

async function scheduleCapsule() {
const title = document.getElementById('schedule-title').value;
const recipient = document.getElementById('schedule-recipient').value;
const releaseDate = document.getElementById('schedule-date').value;
const visibility = document.getElementById('schedule-visibility').value;

if (!title) {
alert('Please enter a title');
return;
}

if (!releaseDate) {
alert('Please select a release date');
return;
}

if (!window.currentMediaUrl) {
alert('No media recorded');
return;
}

if (!window.currentFilename) {
alert('No filename generated');
return;
}

try {
// Convert media URL to blob
console.log('=== FORMDATA BLOB CONVERSION ===');
console.log('Converting media URL to blob:', window.currentMediaUrl);
const response = await fetch(window.currentMediaUrl);
const blob = await response.blob();
console.log('Blob created, size:', blob.size);
console.log('Blob type:', blob.type);

// Create FormData with generated filename
const formData = new FormData();
formData.append('file', blob, window.currentFilename);
console.log('FormData appended with key: file, filename:', window.currentFilename);
formData.append('title', title);
formData.append('content', ''); // No text content for media-only capsules
formData.append('release_date', releaseDate);
formData.append('share_with_email', recipient || '');
formData.append('visibility', visibility);
formData.append('format', window.currentMediaType);

// Submit to API
const apiResponse = await fetch('/api/memories', {
method: 'POST',
credentials: 'include',
body: formData
});

if (apiResponse.ok) {
showSuccess('Capsule scheduled successfully!');
resetRecording();
loadMemories(); // Refresh the vault
} else {
const errorData = await apiResponse.json();
showError('Failed to schedule capsule: ' + (errorData.error || 'Unknown error'));
}
} catch (error) {
console.error('Error scheduling capsule:', error);
showError('Failed to schedule capsule: ' + error.message);
}
}

function updateVideoTimer() {
if (videoTimerInterval) {
clearInterval(videoTimerInterval);
}

videoTimerInterval = setInterval(() => {
if (recordingStartTime) {
const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
const minutes = Math.floor(elapsed / 60);
const seconds = elapsed % 60;

const timerElement = document.getElementById('video-timer');
if (timerElement) {
timerElement.textContent = `Recording: ${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}
}
}, 1000);
}

async function uploadVideo(videoBlob, duration) {
try {
const formData = new FormData();
formData.append('file', videoBlob, `video_${Date.now()}.webm`);
formData.append('memory_id', 'video-story');
formData.append('duration', duration.toString());

console.log('Uploading video:', {
size: videoBlob.size,
duration: duration,
type: videoBlob.type
});

showSuccess('Video recorded and uploaded successfully!');
} catch (error) {
showError('Failed to upload video: ' + error.message);
}
}

// Transmission Modal Functions
function openTransmissionModal(mediaUrl, mediaType) {
document.getElementById('transmission-media-url').value = mediaUrl;
document.getElementById('transmission-media-type').value = mediaType;

// Set minimum date to current time
const now = new Date();
const minDateTime = new Date(now.getTime() + 5 * 60 * 1000); // 5 minutes from now
document.getElementById('transmission-date').min = minDateTime.toISOString().slice(0, 16);
document.getElementById('transmission-date').value = minDateTime.toISOString().slice(0, 16);

// Clear previous form data
document.getElementById('transmission-recipients').value = '';
document.getElementById('transmission-message').value = '';

document.getElementById('transmission-modal').classList.add('active');
}

function closeTransmissionModal() {
document.getElementById('transmission-modal').classList.remove('active');
document.getElementById('transmission-form').reset();
}

function scheduleVideoTransmission(videoUrl) {
openTransmissionModal(videoUrl, 'video');
}

function deleteRecordedMedia(mediaId) {
if (confirm('Are you sure you want to delete this recorded media? This action cannot be undone.')) {
const mediaElement = document.getElementById(mediaId);
if (mediaElement) {
// Remove from DOM
mediaElement.remove();

// Remove from any pending transmissions that reference this media
transmissionQueue = transmissionQueue.filter(transmission => {
if (transmission.mediaUrl && transmission.mediaUrl.includes(mediaId)) {
return false; // Remove transmission
}
return true; // Keep transmission
});

saveToLocalStorage();
showTransmissionQueue();
showSuccess('Media deleted successfully');
}
}
}

function addToTransmissionQueue(videoId) {
const transmission = {
id: generateId(),
videoId: videoId,
recipients: [],
scheduledTime: new Date(Date.now() + 5 * 60 * 1000).toISOString(), // 5 minutes from now
status: 'pending',
createdAt: new Date().toISOString()
};

transmissionQueue.push(transmission);
saveToLocalStorage();
processTransmissionQueue();
}

function processTransmissionQueue() {
transmissionQueue.forEach(async (transmission, index) => {
if (transmission.status === 'pending') {
const now = new Date();
const scheduledTime = new Date(transmission.scheduledTime);

if (now >= scheduledTime) {
await sendVideo(transmission);
}
}
});
// Update the display
showTransmissionQueue();
}

async function sendVideo(transmission) {
try {
// Simulate email sending with video attachment
console.log('Sending video to recipients:', transmission);

// In a real implementation, this would:
// 1. Upload video to temporary storage
// 2. Generate secure download links
// 3. Send emails with download links
// 4. Track delivery status

transmission.status = 'sent';
transmission.sentAt = new Date().toISOString();
saveToLocalStorage();
showTransmissionQueue();

showSuccess(`Video sent to recipients!`);
} catch (error) {
transmission.status = 'failed';
transmission.error = error.message;
saveToLocalStorage();
showTransmissionQueue();

showError('Failed to send video: ' + error.message);
}
}

function showTransmissionQueue() {
const queueElement = document.getElementById('transmission-queue');
if (!queueElement) return;

const pendingTransmissions = transmissionQueue.filter(t => t.status === 'pending');
const sentTransmissions = transmissionQueue.filter(t => t.status === 'sent');
const failedTransmissions = transmissionQueue.filter(t => t.status === 'failed');

if (transmissionQueue.length === 0) {
queueElement.innerHTML = `
<div style="text-align: center; color: var(--text-secondary); padding: 20px;">
No videos in transmission queue
</div>
`;
return;
}

let html = `
<div style="margin-bottom: 20px;">
<h4 style="color: var(--warning-color); margin-bottom: 10px;">📤 Pending Transmissions (${pendingTransmissions.length})</h4>
`;

pendingTransmissions.forEach(transmission => {
const timeUntilSend = Math.max(0, Math.floor((new Date(transmission.scheduledTime) - new Date()) / 1000 / 60));
const mediaIcon = transmission.mediaType === 'video' ? 'Video' : 'Audio';
html += `
<div class="transmission-item">
<div>
<strong>${mediaIcon} Transmission</strong>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Recipients: ${transmission.recipients.join(', ')}
</div>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Scheduled: ${new Date(transmission.scheduledTime).toLocaleString()}
</div>
${transmission.message ? `
<div style="font-size: 0.8rem; color: var(--text-secondary); font-style: italic;">
Message: "${transmission.message}"
</div>
` : ''}
<div style="font-size: 0.7rem; color: var(--warning-color);">
Sending in ${timeUntilSend} minutes
</div>
</div>
<div class="transmission-status pending">Pending</div>
</div>
`;
});

html += `
<div style="margin-bottom: 20px;">
<h4 style="color: var(--success-color); margin-bottom: 10px;">✅ Sent Videos (${sentTransmissions.length})</h4>
`;

sentTransmissions.forEach(transmission => {
const mediaIcon = transmission.mediaType === 'video' ? 'Video' : 'Audio';
html += `
<div class="transmission-item">
<div>
<strong>${mediaIcon} Transmission</strong>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Recipients: ${transmission.recipients.join(', ')}
</div>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Sent: ${new Date(transmission.sentAt).toLocaleString()}
</div>
${transmission.message ? `
<div style="font-size: 0.8rem; color: var(--text-secondary); font-style: italic;">
Message: "${transmission.message}"
</div>
` : ''}
</div>
<div class="transmission-status sent">Delivered</div>
</div>
`;
});

if (failedTransmissions.length > 0) {
html += `
<div style="margin-bottom: 20px;">
<h4 style="color: var(--error-color); margin-bottom: 10px;">❌ Failed Transmissions (${failedTransmissions.length})</h4>
</div>
`;

failedTransmissions.forEach(transmission => {
const mediaIcon = transmission.mediaType === 'video' ? 'Video' : 'Audio';
html += `
<div class="transmission-item">
<div>
<strong>${mediaIcon} Transmission</strong>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Recipients: ${transmission.recipients.join(', ')}
</div>
<div style="font-size: 0.8rem; color: var(--text-secondary);">
Error: ${transmission.error}
</div>
${transmission.message ? `
<div style="font-size: 0.8rem; color: var(--text-secondary); font-style: italic;">
Message: "${transmission.message}"
</div>
` : ''}
</div>
<div class="transmission-status failed">Failed</div>
</div>
`;
});
}

queueElement.innerHTML = html;
}

// Storytelling Engine
function rotateStoryPrompts() {
let currentIndex = 0;

setInterval(() => {
currentIndex = (currentIndex + 1) % storyPrompts.length;
const promptElement = document.getElementById('story-prompt');
promptElement.style.opacity = '0';

setTimeout(() => {
promptElement.textContent = `"${storyPrompts[currentIndex]}"`;
promptElement.style.opacity = '1';
}, 300);
}, 10000); // Rotate every 10 seconds
}

async function uploadStoryAudio(audioBlob) {
try {
const formData = new FormData();
formData.append('file', audioBlob, 'story.wav');
formData.append('memory_id', 'story-recording');

console.log('Uploading audio story:', {
size: audioBlob.size,
type: audioBlob.type
});

showSuccess('Audio story recorded and uploaded successfully!');
} catch (error) {
showError('Failed to upload story: ' + error.message);
}
}

// Local Storage Management
function saveToLocalStorage() {
localStorage.setItem('timeCapsuleVideos', JSON.stringify(recordedVideos));
localStorage.setItem('timeCapsuleTransmissions', JSON.stringify(transmissionQueue));
}

function loadFromLocalStorage() {
const savedVideos = localStorage.getItem('timeCapsuleVideos');
if (savedVideos) {
recordedVideos = JSON.parse(savedVideos);
}

const savedTransmissions = localStorage.getItem('timeCapsuleTransmissions');
if (savedTransmissions) {
transmissionQueue = JSON.parse(savedTransmissions);
}
}

// Form Submission
const createForm = document.getElementById('create-form');
if (createForm) {
createForm.addEventListener('submit', function(e) {
console.log('BUTTON CLICKED');
console.log('=== Create Capsule Form Submitted ===');
e.preventDefault();
console.log('Form submission prevented');

const format = document.querySelector('input[name="capsule-format"]:checked').value;
console.log('Selected format:', format);
let content = '';
let mediaBlob = null;
let mediaType = null;
let hasVideo = false;
let hasAudio = false;

if (format === 'text') {
content = document.getElementById('memory-content').value;
console.log('Text content length:', content.length);
if (!content.trim()) {
showError('Please write your time capsule content.');
return;
}
} else if (format === 'audio') {
console.log('Audio format selected, checking blob...');
console.log('capsuleCurrentAudioBlob:', capsuleCurrentAudioBlob);
if (!capsuleCurrentAudioBlob) {
showError('Please record your audio time capsule.');
return;
}
mediaBlob = capsuleCurrentAudioBlob;
mediaType = 'audio/webm';
content = 'Audio time capsule recorded';
hasAudio = true;
} else if (format === 'video') {
console.log('Video format selected, checking blob...');
console.log('capsuleCurrentVideoBlob:', capsuleCurrentVideoBlob);
if (!capsuleCurrentVideoBlob) {
console.error('No video found!');
showError('Please record your video time capsule.');
return;
}
mediaBlob = capsuleCurrentVideoBlob;
mediaType = 'video/webm';
content = 'Video time capsule recorded';
hasVideo = true;
}

const formData = {
id: generateId(),
title: document.getElementById('memory-title').value,
content: content,
release_date: document.getElementById('release-date').value,
share_with_email: document.getElementById('share-with-email').value,
visibility: document.getElementById('memory-visibility').value,
created_at: new Date().toISOString(),
is_released: false,
has_video: hasVideo,
has_audio: hasAudio,
has_image: false,
format: format,
mediaType: mediaType,
mediaUrl: mediaBlob ? URL.createObjectURL(mediaBlob) : null,
status: 'locked'
};

console.log('Form data collected:', {
title: formData.title,
content: formData.content,
release_date: formData.release_date,
share_with_email: formData.share_with_email,
visibility: formData.visibility,
format: formData.format,
hasVideo: hasVideo,
hasAudio: hasAudio,
mediaBlob: !!mediaBlob
});

// Send to backend API
if (mediaBlob) {
console.log('=== Starting video/audio upload ===');
console.log('Media blob size:', mediaBlob.size);
console.log('Media type:', mediaType);
console.log('Format:', format);

// Validate media blob size
if (mediaBlob.size === 0) {
showError('Media file is empty. Please record again.');
return;
}

// Validate media blob size (max 100MB)
if (mediaBlob.size > 100 * 1024 * 1024) {
showError('Media file is too large. Maximum size is 100MB.');
return;
}

// Use FormData for file uploads
const uploadFormData = new FormData();
uploadFormData.append('title', formData.title);
uploadFormData.append('content', formData.content);
uploadFormData.append('release_date', formData.release_date);
uploadFormData.append('share_with_email', formData.share_with_email);
uploadFormData.append('visibility', formData.visibility);
uploadFormData.append('format', formData.format);
uploadFormData.append('media', mediaBlob, `capsule_${formData.id}.${mediaType.split('/')[1]}`);

console.log('FormData prepared with fields:', {
title: formData.title,
content: formData.content,
release_date: formData.release_date,
share_with_email: formData.share_with_email,
visibility: formData.visibility,
format: formData.format
});

fetch('/api/memories', {
method: 'POST',
credentials: 'include',
body: uploadFormData
})
.then(response => {
console.log('Response received:', response.status, response.statusText);
if (!response.ok) {
return response.json().then(err => {
throw new Error(err.error || 'Upload failed');
});
}
return response.json();
})
.then(data => {
console.log('Memory created on server:', data);
showSuccess('Time capsule created successfully!');
closeCreateModal();
loadMemories();
})
.catch(error => {
console.error('Error creating memory:', error);
console.error('Error details:', error.message);
showError('Failed to upload media: ' + error.message);
});
} else {
// Use JSON for text-only memories
fetch('/api/memories', {
method: 'POST',
headers: {
'Content-Type': 'application/json'
},
credentials: 'include',
body: JSON.stringify({
title: formData.title,
content: formData.content,
release_date: formData.release_date,
share_with_email: formData.share_with_email,
visibility: formData.visibility,
format: formData.format
})
})
.then(response => response.json())
.then(data => {
console.log('Memory created on server:', data);
showSuccess('Time capsule created successfully!');
closeCreateModal();
loadMemories();
})
.catch(error => {
console.error('Error creating memory:', error);
showError('Failed to create memory: ' + error.message);
});
}
});
}

// Transmission Form Submission
const transmissionForm = document.getElementById('transmission-form');
if (transmissionForm) {
transmissionForm.addEventListener('submit', function(e) {
e.preventDefault();

const mediaUrl = document.getElementById('transmission-media-url').value;
const mediaType = document.getElementById('transmission-media-type').value;
const scheduledDate = document.getElementById('transmission-date').value;
const recipients = document.getElementById('transmission-recipients').value;
const message = document.getElementById('transmission-message').value;

// Validation
const scheduledTime = new Date(scheduledDate);
const now = new Date();

if (scheduledTime <= now) {
showError('Transmission date must be in the future');
return;
}

// Parse and validate emails
const emailList = recipients.split(',').map(email => email.trim()).filter(email => email);
const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

for (const email of emailList) {
if (!emailRegex.test(email)) {
showError(`Invalid email format: ${email}`);
return;
}
}

if (emailList.length === 0) {
showError('At least one recipient email is required');
return;
}

// Create transmission
const transmission = {
id: generateId(),
mediaUrl: mediaUrl,
mediaType: mediaType,
recipients: emailList,
message: message,
scheduledTime: scheduledTime.toISOString(),
status: 'pending',
createdAt: new Date().toISOString()
};

transmissionQueue.push(transmission);
saveToLocalStorage();
processTransmissionQueue();

closeTransmissionModal();
showSuccess(`Transmission scheduled for ${scheduledTime.toLocaleString()} to ${emailList.length} recipient(s)!`);
});
}

// Family Member Form Submission
const familyMemberForm = document.getElementById('family-member-form');
if (familyMemberForm) {
familyMemberForm.addEventListener('submit', function(e) {
e.preventDefault();

const memberId = document.getElementById('family-member-id').value;
const name = document.getElementById('family-member-name').value;
const parentId = document.getElementById('family-member-parent').value;
const relationship = document.getElementById('family-member-relationship').value;
const siblingOf = document.getElementById('family-member-sibling-of').value;
const spouseOf = document.getElementById('family-member-spouse-of').value;
const birthYear = document.getElementById('family-member-birth-year').value;
const photoUrl = document.getElementById('family-member-photo-url').value;
const bio = document.getElementById('family-member-bio').value;

console.log('Form submitted - memberId:', memberId, 'name:', name, 'mode:', memberId ? 'EDIT' : 'ADD');

// Handle sibling relationship - if sibling, set parent to the sibling's parent
let finalParentId = parentId;
if (relationship === 'sibling' && siblingOf) {
const sibling = familyMembers.find(m => m.id === siblingOf);
if (sibling && sibling.parentId) {
finalParentId = sibling.parentId;
}
}

// Handle profile picture file upload
const photoFile = document.getElementById('family-member-photo-file');
let photoData = photoUrl || null;

if (photoFile.files && photoFile.files[0]) {
const reader = new FileReader();
reader.onload = function(e) {
photoData = e.target.result;
const linkedAccount = document.getElementById('family-member-linked-account').value;
const isAlive = document.getElementById('family-member-alive').value === 'true';
saveFamilyMember(memberId, name, finalParentId, relationship, birthYear, photoData, bio, siblingOf, spouseOf, linkedAccount, isAlive);
};
reader.readAsDataURL(photoFile.files[0]);
} else {
const linkedAccount = document.getElementById('family-member-linked-account').value;
const isAlive = document.getElementById('family-member-alive').value === 'true';
saveFamilyMember(memberId, name, finalParentId, relationship, birthYear, photoData, bio, siblingOf, spouseOf, linkedAccount, isAlive);
}
});
}

function saveFamilyMember(memberId, name, parentId, relationship, birthYear, photoData, bio, siblingOf, spouseOf, linkedAccount, isAlive) {
if (memberId) {
// Update existing member
const member = familyMembers.find(m => m.id === memberId);
if (member) {
member.name = name;
member.parentId = parentId || null;
member.relationship = relationship;
member.birthYear = birthYear || null;
member.photo = photoData || null;
member.bio = bio || null;
member.siblingOf = siblingOf || null;
member.spouseOf = spouseOf || null;
member.linkedAccount = linkedAccount || null;
member.isAlive = isAlive !== undefined ? isAlive : true;
}
} else {
// Add new member
console.log('Creating new member with parentId:', parentId);
const newMember = {
id: generateId(),
name: name,
parentId: parentId || null,
relationship: relationship,
birthYear: birthYear || null,
photo: photoData || null,
bio: bio || null,
siblingOf: siblingOf || null,
spouseOf: spouseOf || null,
linkedAccount: linkedAccount || null,
isAlive: isAlive !== undefined ? isAlive : true,
autobiography: null,
createdAt: new Date().toISOString()
};

familyMembers.push(newMember);
console.log('Current Tree Array:', familyMembers);
}

saveFamilyMembers();
console.log('Data saved to localStorage. Current localStorage familyMembers:', localStorage.getItem('familyMembers'));
window.location.reload();
closeFamilyMemberModal();
renderFamilyTree();
showSuccess(memberId ? 'Family member updated successfully!' : 'Family member added successfully!');
}

// Utility Functions
function generateId() {
return 'mem_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
}

function formatDate(dateString) {
const date = new Date(dateString);
return date.toLocaleDateString('en-US', {
year: 'numeric',
month: 'short',
day: 'numeric',
hour: '2-digit',
minute: '2-digit'
});
}

function showError(message) {
const errorDiv = document.createElement('div');
errorDiv.className = 'error';
errorDiv.textContent = message;
document.querySelector('.container').insertBefore(errorDiv, document.querySelector('.main-content'));

setTimeout(() => errorDiv.remove(), 5000);
}

function showSuccess(message) {
const successDiv = document.createElement('div');
successDiv.className = 'success';
successDiv.textContent = message;
document.querySelector('.container').insertBefore(successDiv, document.querySelector('.main-content'));

setTimeout(() => successDiv.remove(), 5000);
}

// Close modals on escape key
document.addEventListener('keydown', function(e) {
if (e.key === 'Escape') {
closeCreateModal();
closeMemoryModal();
closeTransmissionModal();
closeFamilyMemberModal();
closeAutobiographyModal();
}
});

// Close modals on background click
const createModal = document.getElementById('create-modal');
if (createModal) {
createModal.addEventListener('click', function(e) {
if (e.target === this) {
closeCreateModal();
}
});
}

const memoryModal = document.getElementById('memory-modal');
if (memoryModal) {
memoryModal.addEventListener('click', function(e) {
if (e.target === this) {
closeMemoryModal();
}
});
}

const transmissionModal = document.getElementById('transmission-modal');
if (transmissionModal) {
transmissionModal.addEventListener('click', function(e) {
if (e.target === this) {
closeTransmissionModal();
}
});
}

const familyMemberModal = document.getElementById('family-member-modal');
if (familyMemberModal) {
familyMemberModal.addEventListener('click', function(e) {
if (e.target === this) {
closeFamilyMemberModal();
}
});
}

const autobiographyModal = document.getElementById('autobiography-modal');
if (autobiographyModal) {
autobiographyModal.addEventListener('click', function(e) {
if (e.target === this) {
closeAutobiographyModal();
}
});
}

const referralCodeModal = document.getElementById('referral-code-modal');
if (referralCodeModal) {
referralCodeModal.addEventListener('click', function(e) {
if (e.target === this) {
closeReferralCodeModal();
}
});
}

const familyTreeSettingsModal = document.getElementById('family-tree-settings-modal');
if (familyTreeSettingsModal) {
familyTreeSettingsModal.addEventListener('click', function(e) {
if (e.target === this) {
closeFamilyTreeSettings();
}
});
}

const deleteConfirmationModal = document.getElementById('delete-confirmation-modal');
if (deleteConfirmationModal) {
deleteConfirmationModal.addEventListener('click', function(e) {
if (e.target === this) {
closeDeleteConfirmationModal();
}
});
}

// Close dropdowns when clicking outside
document.addEventListener('click', function(e) {
if (!e.target.closest('.tree-add-relative-btn') && !e.target.closest('.add-relative-dropdown')) {
document.querySelectorAll('.add-relative-dropdown').forEach(dropdown => {
dropdown.style.display = 'none';
dropdown.classList.remove('dropdown-down');
});
}
});

// Private Stories Modal Events
const privateStoryModal = document.getElementById('private-story-modal');
if (privateStoryModal) {
privateStoryModal.addEventListener('click', function(e) {
if (e.target === this) {
closeStoryModal();
}
});
}
