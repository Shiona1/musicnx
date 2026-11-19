import { mount } from 'svelte';
import '../styles/tokens.css';
import '../styles/global.css';
import '../styles/materials.css';
import MaterialsPreview from './MaterialsPreview.svelte';

const target = document.getElementById('material-preview');
if (target) mount(MaterialsPreview, { target });
