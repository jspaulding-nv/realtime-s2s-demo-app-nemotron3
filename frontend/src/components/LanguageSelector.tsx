import type { Language } from '../types/messages';

interface LanguageSelectorProps {
  languages: Language[];
  selectedLanguage: string;
  onChange: (languageCode: string) => void;
  disabled?: boolean;
}

export function LanguageSelector({
  languages,
  selectedLanguage,
  onChange,
  disabled = false,
}: LanguageSelectorProps) {
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor="language-select"
        className="text-sm font-medium text-gray-700"
      >
        Translate to:
      </label>
      <select
        id="language-select"
        value={selectedLanguage}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="px-3 py-2 border border-gray-300 rounded-lg bg-white text-gray-900
                   focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500
                   disabled:bg-gray-100 disabled:cursor-not-allowed
                   cursor-pointer"
      >
        {languages.map((lang) => (
          <option
            key={lang.code}
            value={lang.code}
            disabled={!lang.available}
          >
            {lang.name} {!lang.available && '(unavailable)'}
          </option>
        ))}
      </select>
    </div>
  );
}
