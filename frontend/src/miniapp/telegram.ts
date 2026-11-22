/** The slice of Telegram's WebApp bridge this app uses. */

export interface TelegramWebApp {
  initData: string
  ready(): void
  expand(): void
  openLink(url: string): void
  openTelegramLink(url: string): void
  showAlert(message: string): void
  HapticFeedback?: { impactOccurred(style: 'light' | 'medium'): void }
  BackButton: {
    show(): void
    hide(): void
    onClick(callback: () => void): void
    offClick(callback: () => void): void
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export function tg(): TelegramWebApp | undefined {
  return window.Telegram?.WebApp
}

/** Open a link the way Telegram wants: t.me links inside Telegram, others outside. */
export function open(url: string): void {
  const app = tg()
  if (!app) {
    window.open(url, '_blank', 'noopener')
    return
  }
  if (/^https:\/\/t\.me\//.test(url)) app.openTelegramLink(url)
  else app.openLink(url)
}

export function notify(message: string): void {
  const app = tg()
  if (app) app.showAlert(message)
  else window.alert(message)
}

export function tap(): void {
  tg()?.HapticFeedback?.impactOccurred('light')
}
