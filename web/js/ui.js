// Toast notifications and the MeshMapper deep-link modal.

import * as api from './api.js';

const $ = (id) => document.getElementById(id);
let toastTimer = null;

export function showToast(message) {
  const toast = $('toast');
  toast.innerText = message;
  toast.style.display = 'block';
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

export async function openMeshMapperModal() {
  $('meshmapper-modal').classList.add('active');
  const input = $('meshmapper-url-input');
  input.value = 'Loading…';
  try {
    input.value = await api.getMeshMapperLink();
  } catch {
    input.value = 'Could not load link — check the DedDrop logs.';
  }
}

export function closeMeshMapperModal() {
  $('meshmapper-modal').classList.remove('active');
}

export function copyMeshMapperLink() {
  const input = $('meshmapper-url-input');
  input.select();
  const done = () => showToast('MeshMapper deep link copied to clipboard!');
  const failed = () => showToast('Copy failed — select the text and copy manually.');

  // The clipboard API is unavailable on plain-http origins other than
  // localhost, which is exactly how this dashboard is reached over a LAN.
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(input.value).then(done, failed);
  } else if (document.execCommand('copy')) {
    done();
  } else {
    failed();
  }
}
