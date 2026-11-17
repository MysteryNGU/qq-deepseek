"""Optional Live2D-only Qt process. No model calls, database or character identity."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import sys
import threading


def emit(kind, **fields):
    # Windows pythonw and console launchers may use different stdout codepages.
    print('@companion-pet '+json.dumps(dict(type=kind, **fields), ensure_ascii=True), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Optional Live2D desktop renderer')
    parser.add_argument('--model', required=True)
    parser.add_argument('--height', type=int, default=460)
    parser.add_argument('--capture', help='Save a transparent framebuffer PNG for local rendering checks')
    parser.add_argument('--test-seconds', type=float, default=0)
    args = parser.parse_args(argv)
    try:
        import live2d
        from OpenGL import GL
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QCursor, QSurfaceFormat
        from PySide6.QtOpenGLWidgets import QOpenGLWidget
        from PySide6.QtWidgets import QApplication, QMenu
    except ImportError as error:
        emit('error', text='Live2D 依赖未安装，请安装 requirements-desktop.txt：'+str(error))
        return 1
    path = Path(args.model).resolve()
    try:
        data = json.loads(path.read_text('utf-8'))
        references = data['FileReferences']
        for resource in [references['Moc'], *references['Textures']]:
            if not (path.parent/resource).is_file():
                raise ValueError('模型资源缺失：'+resource)
    except (OSError, ValueError, KeyError) as error:
        emit('error', text=str(error))
        return 1

    format = QSurfaceFormat()
    format.setAlphaBufferSize(8)
    format.setDepthBufferSize(24)
    format.setStencilBufferSize(8)
    format.setSamples(4)
    QSurfaceFormat.setDefaultFormat(format)
    app = QApplication(sys.argv[:1])
    live2d.init()
    live2d.enableLog(False)
    inbox = queue.Queue()

    def read_commands():
        for line in sys.stdin:
            try:
                inbox.put(json.loads(line))
            except ValueError:
                continue
        inbox.put(dict(type='close'))

    class Pet(QOpenGLWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('Live2D · 桌面伙伴')
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
            self.model = None
            self.failed = False
            self.captured = False
            self.frames = 0
            self.drag_start = None
            self.set_height(args.height)
            area = app.primaryScreen().availableGeometry()
            self.move(area.right()-self.width()-35, area.bottom()-self.height())
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(33)

        def set_height(self, height):
            height = max(220, min(900, int(height)))
            self.resize(round(height*.72), height)

        def initializeGL(self):
            try:
                live2d.glInit()
                self.model = live2d.Model()
                self.model.LoadModelJson(str(path))
                self.model.SetAutoBlink(True)
                self.model.SetAutoBreath(True)
                self.model.Resize(self.width(), self.height())
                if references.get('Motions', {}).get('Idle'):
                    self.model.StartMotion('Idle', 0, live2d.MotionPriority.IDLE)
            except Exception as error:
                self.failed = True
                emit('error', text='模型加载失败：'+str(error))
                QTimer.singleShot(0, app.quit)

        def resizeGL(self, width, height):
            if self.model:
                self.model.Resize(width, height)

        def paintGL(self):
            if self.failed or not self.model:
                return
            try:
                GL.glClearColor(0, 0, 0, 0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
                self.model.Update()
                self.model.Draw()
                self.frames += 1
                if self.frames == 2:
                    emit('ready', model=str(path), width=self.width(), height=self.height(),
                         hwnd=int(self.winId()), x=self.x(), y=self.y(), alpha_buffer=self.format().alphaBufferSize())
                if args.capture and self.frames >= 45 and not self.captured:
                    self.captured = True
                    QTimer.singleShot(0, self.capture)
            except Exception as error:
                self.failed = True
                emit('error', text='模型绘制失败：'+str(error))
                QTimer.singleShot(0, app.quit)

        def capture(self):
            self.grabFramebuffer().save(args.capture)
            emit('capture', path=args.capture, frames=self.frames)

        def tick(self):
            try:
                while True:
                    event = inbox.get_nowait()
                    if event.get('type') == 'close':
                        app.quit()
                    elif event.get('type') == 'resize':
                        self.set_height(event['height'])
            except queue.Empty:
                pass
            if self.model and not self.failed:
                cursor = self.mapFromGlobal(QCursor.pos())
                self.model.Drag(max(0, min(self.width(), cursor.x())), max(0, min(self.height(), cursor.y())))
            self.update()

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self.drag_start = event.globalPosition().toPoint()-self.pos()

        def mouseMoveEvent(self, event):
            if self.drag_start is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint()-self.drag_start)

        def mouseReleaseEvent(self, event):
            self.drag_start = None

        def mouseDoubleClickEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                emit('open_chat')

        def contextMenuEvent(self, event):
            menu = QMenu(self)
            for label, kind in [('打开对话', 'open_chat'), ('选择模型…', 'choose_model'), ('切换自主活动', 'toggle')]:
                menu.addAction(label, lambda checked=False, value=kind: emit(value))
            menu.addSeparator()
            menu.addAction('放大', lambda: emit('scale', factor=1.15))
            menu.addAction('缩小', lambda: emit('scale', factor=1/1.15))
            menu.addSeparator()
            menu.addAction('退出伙伴', lambda: emit('exit'))
            menu.exec(event.globalPos())

    window = Pet()
    threading.Thread(target=read_commands, name='pet-input', daemon=True).start()
    window.show()
    if args.test_seconds:
        QTimer.singleShot(round(args.test_seconds*1000), app.quit)
    try:
        return app.exec()
    finally:
        if args.test_seconds:
            emit('position', x=window.x(), y=window.y())
        window.makeCurrent()
        if window.model:
            window.model.DestroyRenderer()
            window.model = None
        live2d.dispose()
        window.doneCurrent()


if __name__ == '__main__':
    raise SystemExit(main())
