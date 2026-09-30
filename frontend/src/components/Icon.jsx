// Stroke icon set (24px grid, currentColor). Keeps the UI free of emoji,
// which render differently on every OS.

const ICONS = {
  logo: <><circle cx="12" cy="12" r="3.2" /><ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(-28 12 12)" /></>,
  grid: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
  sliders: <><path d="M3 6h11M18 6h3M3 12h5M12 12h9M3 18h13M20 18h1" /><circle cx="16" cy="6" r="2" /><circle cx="10" cy="12" r="2" /><circle cx="18" cy="18" r="2" /></>,
  target: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1" fill="currentColor" /></>,
  moon: <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" />,
  flask: <path d="M9 3h6M10 3v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7.5 15h9" />,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>,
  code: <path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13.5 4l-3 16" />,
  external: <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />,
  chevronLeft: <path d="M15 18l-6-6 6-6" />,
  chevronRight: <path d="M9 18l6-6-6-6" />,
  chevronDown: <path d="M6 9l6 6 6-6" />,
  x: <path d="M18 6L6 18M6 6l12 12" />,
  check: <path d="M20 6L9 17l-5-5" />,
  minus: <path d="M5 12h14" />,
  undo: <path d="M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />,
  redo: <path d="M15 14l5-5-5-5M20 9H9.5a5.5 5.5 0 0 0 0 11H13" />,
  scan: <><path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2" /><circle cx="12" cy="12" r="3.5" /></>,
  columns: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M12 4v16" /></>,
  crosshair: <><circle cx="12" cy="12" r="8" /><path d="M12 2v5M12 17v5M2 12h5M17 12h5" /></>,
  play: <path d="M7 4.5v15l12-7.5z" />,
  download: <path d="M12 3v12M7 10l5 5 5-5M4 21h16" />,
  folder: <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />,
  search: <><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></>,
  plus: <path d="M12 5v14M5 12h14" />,
  trash: <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" />,
  refresh: <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />,
  alert: <path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01" />,
  streak: <><path d="M4 20L20 4" strokeDasharray="3 3" /><circle cx="7" cy="7" r="1" fill="currentColor" /><circle cx="17" cy="17" r="1" fill="currentColor" /></>,
  copy: <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" /></>,
  arrowUp: <path d="M12 19V5M5 12l7-7 7 7" />,
  arrowDown: <path d="M12 5v14M19 12l-7 7-7-7" />,
  bookmark: <path d="M6 3h12v18l-6-4-6 4z" />,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 16v-4M12 8h.01" /></>,
  image: <><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><path d="M21 15l-5-5L5 21" /></>,
  activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  list: <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />,
  file: <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6" />,
  pin: <><path d="M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z" /><circle cx="12" cy="9.5" r="2.5" /></>,
  wrench: <path d="M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8a2.8 2.8 0 0 1-4-4l8-8-1.3-1.3a4 4 0 0 0-5-5l2.6 2.6-1.4 3-3 1.4L6.3 7.1" />,
  gauge: <><path d="M12 14l4-4" /><path d="M3.5 18a10 10 0 1 1 17 0" /></>,
}

export default function Icon({ name, size = 16, className = '', strokeWidth = 1.75, ...rest }) {
  return (
    <svg className={`icon ${className}`} width={size} height={size} viewBox="0 0 24 24"
         fill="none" stroke="currentColor" strokeWidth={strokeWidth}
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {ICONS[name]}
    </svg>
  )
}
