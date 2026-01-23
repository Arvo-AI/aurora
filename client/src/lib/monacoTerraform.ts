export function configureMonaco(monaco: any) {
  if (!monaco) return;
  
  // Define custom black theme
  if (!monaco.editor.getTheme || !monaco.editor.getTheme('terraform-dark')) {
    monaco.editor.defineTheme('terraform-dark', {
      base: 'vs-dark',
      inherit: true,
      rules: [
        { token: '', foreground: 'ffffff', background: '000000' },
        { token: 'string', foreground: 'ce9178' },
        { token: 'keyword', foreground: '569cd6' },
        { token: 'number', foreground: 'b5cea8' },
        { token: 'comment', foreground: '6a9955' },
        { token: 'type', foreground: '4ec9b0' },
        { token: 'keyword.control', foreground: 'c586c0' },
      ],
      colors: {
        'editor.background': '#000000',
        'editor.foreground': '#ffffff',
        'editor.lineHighlightBackground': '#0a0a0a',
        'editorLineNumber.foreground': '#666666',
        'editorLineNumber.activeForeground': '#ffffff',
        'editorIndentGuide.background': '#404040',
        'editorIndentGuide.activeBackground': '#707070',
      }
    });
  }
  
  if (!monaco.languages.getLanguages().some((lang: any) => lang.id === 'terraform')) {
    monaco.languages.register({ id: 'terraform' });
    monaco.languages.setMonarchTokensProvider('terraform', {
      tokenizer: {
        root: [
          [/#.*$/, 'comment'],
          [/\/\*/, 'comment', '@comment'],
          [/".*?"/, 'string'],
          [/'.*?'/, 'string'],
          [/\d+(\.\d+)?/, 'number'],
          [/\b(resource|data|variable|output|locals|module|provider|terraform|import|moved)\b/, 'keyword'],
          [/\b(string|number|bool|list|map|set|object|tuple|any)\b/, 'type'],
          [/\b(count|for_each|depends_on|lifecycle|provisioner)\b/, 'keyword.control'],
          [/\{|\}|\(|\)|\[|\]/, '@brackets'],
          [/[^\s]+/, 'identifier'],
        ],
        comment: [
          [/[^\/*]+/, 'comment'],
          [/\*\//, 'comment', '@pop'],
          [/\/\*/, 'comment', '@comment']
        ],
      },
    });
    monaco.languages.setLanguageConfiguration('terraform', {
      comments: { lineComment: '#', blockComment: ['/*', '*/'] },
      brackets: [ ['{', '}'], ['[', ']'], ['(', ')'] ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
      ],
    });
  }
}
