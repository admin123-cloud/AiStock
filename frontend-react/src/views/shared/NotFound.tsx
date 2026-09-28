import { Button } from '@/components/catalyst/button'

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center rounded-2xl bg-gradient-to-br from-indigo-400 to-purple-800 p-5">
      <div className="w-full max-w-xl rounded-2xl bg-white p-10 text-center shadow-2xl dark:bg-zinc-900 sm:p-16">
        <div className="mb-5 text-6xl">🔍</div>
        <h1 className="mb-4 bg-gradient-to-br from-indigo-400 to-purple-800 bg-clip-text text-6xl font-bold text-transparent sm:text-8xl">
          404
        </h1>
        <p className="mb-8 text-lg text-zinc-500 dark:text-zinc-400">抱歉，页面未找到</p>
        <Button href="/">返回首页</Button>
      </div>
    </div>
  )
}
