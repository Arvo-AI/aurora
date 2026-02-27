import React from 'react';
import type { Components } from 'react-markdown';

export const postmortemMarkdownComponents: Components = {
  h1: ({ children }) => <h1 className="text-base font-semibold text-white mb-2">{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold text-white mt-4 mb-2">{children}</h2>,
  h3: ({ children }) => <h3 className="text-sm font-medium text-zinc-200 mt-3 mb-1">{children}</h3>,
  strong: ({ children }) => <strong className="text-orange-300 font-semibold">{children}</strong>,
  p: ({ children }) => <p className="mb-2 text-zinc-300 text-sm leading-normal">{children}</p>,
  ul: ({ children }) => <ul className="list-disc list-outside ml-4 mb-2 space-y-1">{children}</ul>,
  li: ({ children }) => <li className="text-zinc-300 text-sm">{children}</li>,
  code: ({ children }) => <code className="bg-zinc-800 px-1.5 py-0.5 rounded text-orange-300 text-xs font-mono">{children}</code>,
};
