/** A quiet header utility over the accessible native language picker. */
import {escapeHtml as esc} from './lib.mjs';
import {getLocale,normalizeLocale,localeName,SUPPORTED_LOCALES,t} from './i18n.mjs';

const abbreviations=Object.freeze({en:'EN',ko:'KO',ja:'JA','zh-Hant':'繁中'});
export function languageControlMarkup(id,locale=getLocale(),disabled=false){
  const selected=normalizeLocale(locale);
  return `<label class="language-control" for="${esc(id)}" title="${esc(t('common.language',{},selected)+': '+localeName(selected))}">
    <span class="language-trigger" aria-hidden="true"><svg class="language-globe" viewBox="0 0 24 24"><path d="M2 12h20M12 2c-6 6-6 14 0 20 6-6 6-14 0-20ZM22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0"/></svg><span class="language-code">${abbreviations[selected]}</span><svg class="language-chevron" viewBox="0 0 12 12"><path d="m3 4.5 3 3 3-3"/></svg></span>
    <select id="${esc(id)}" class="language-select" data-language-select aria-label="${esc(t('common.language',{},selected))}" ${disabled?'disabled':''}>${SUPPORTED_LOCALES.map(item=>`<option value="${item.code}" ${item.code===selected?'selected':''}>${item.name}</option>`).join('')}</select>
  </label>`;
}
