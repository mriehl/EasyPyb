#   The MIT License (MIT)

#   Copyright (c) 2014-2015 Maximilien Riehl <max@riehl.io>

#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:

#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.

#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#   THE SOFTWARE.

import os
import subprocess
import sys
import threading
import fcntl
import errno
import select

import sublime
import sublime_plugin

global panel  # ugly - but view.get_output_panel recreates the output panel
              # each time it is called, which sucks
panel = None


class ExecutionError(BaseException):

    def __str__(self):
        message = super(ExecutionError, self).__str__()
        return '''
An error has occurred while trying to run PyBuilder!


{0}
'''.format(message)


class PybRun(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors([])


class PybClean(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['clean'])


class PybRunUnitTests(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['run_unit_tests'])


class PybRunIntegrationTests(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['run_integration_tests'])


class PybAnalyze(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['analyze'])


class PybVerify(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['verify'])


class PybPublish(sublime_plugin.ApplicationCommand):

    def run(self):
        run_pybuilder_and_catch_errors(['publish'])


class PybInit(sublime_plugin.ApplicationCommand):

    def run(self):
        pyb_init()


class ScratchText(sublime_plugin.TextCommand):
    """
    Helper command to deploy text to the sublime_pybuilder output panel.
    Also gives focus to the panel if it's not focused yet.
    The panel needs to be a global because get_output_panel'ing it recreates
    it and discards the existing text.
    """

    def run(self, edit, text):
        window = sublime.active_window()
        panel.insert(edit, panel.size(), text)
        panel.show(panel.size())
        panel_active = panel.id() == window.active_view().id()
        if not panel_active:
            window.run_command("show_panel", {"panel": "output.easypyb"})


def run_pybuilder_and_catch_errors(pyb_args):
        try:
            run_pybuilder(pyb_args)
        except ExecutionError as error:
            sublime.error_message(str(error))


def run_pybuilder(pyb_args):
    project_root = get_project_root()

    pyb_script = determine_pyb_executable_command()
    pyb_script.extend(pyb_args)

    scratch('Build started...', new_panel=True, newline=True)

    defer_with_progress(pyb_script, cwd=project_root)


def determine_pyb_executable_command():
    interpreter = get_setting('python_interpreter')

    pyb_path = get_setting('pyb_path', mandatory=False)
    if pyb_path:
        return [interpreter, pyb_path]
    return infer_pyb_executable_command_from_interpreter(interpreter)


def infer_pyb_executable_command_from_interpreter(interpreter):
    bin_dir = os.path.dirname(interpreter)
    pyb_script = os.path.join(bin_dir, 'pyb')
    if not os.path.exists(pyb_script):
        error_message = 'Cannot find PyBuilder at {0}, perhaps it is not installed?'.format(
            pyb_script)
        raise ExecutionError(error_message)

    return [pyb_script]


def defer_with_progress(args, cwd=None, shell=False):
    thread = threading.Thread(
        target=spawn_command_with_realtime_output, args=(args, cwd, shell))
    thread.start()
    ThreadProgress(thread, 'PyBuilder running', 'PyBuilder finished!')


def spawn_command_with_realtime_output(args, cwd, shell):
    venv_bin_dir = os.path.dirname(get_setting('python_interpreter'))
    env = os.environ
    env['PATH'] += ':%s' % venv_bin_dir
    child = subprocess.Popen(
        args, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=shell, env=os.environ)
    flag_fd_as_async(child.stdout)
    flag_fd_as_async(child.stderr)

    while True:
        select.select([child.stdout, child.stderr], [], [])

        stdout = read_async(child.stdout)
        stderr = read_async(child.stderr)

        if stdout:
            scratch(stdout.decode('utf-8'))
        if stderr:
            scratch(stderr.decode('utf-8'))

        finished = child.poll() is not None

        if finished:
            return


def scratch(text, new_panel=False, newline=False):
    global panel
    if new_panel:
        window = sublime.active_window()
        panel = window.get_output_panel("sublime_pybuilder")
    if newline:
        text += '\n'
    sublime.active_window().run_command('scratch_text', {'text': text})


def flag_fd_as_async(fd):
    fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(
        fd, fcntl.F_GETFL) | os.O_NONBLOCK)


def read_async(fd):
    try:
        return fd.read()
    except IOError as e:
        if e.errno == errno.EAGAIN:
            return ''
        raise e


def plugin_loaded():
    if sys.version_info < (3, 3):
        error = 'sublime_pybuilder is only compatible with Sublime Text 3'
        sublime.error_message(error)
        raise RuntimeError(error)


def plugin_unloaded():
    pass


def pyb_init():
    project_root = get_project_root()

    scratch('Pyb init started...', new_panel=True, newline=True)

    defer_with_progress(['pyb-init local'], cwd=project_root, shell=True)


def get_setting(name, mandatory=True):
    window = sublime.active_window()
    view = window.active_view()

    setting = view.settings().get(name)
    if not setting and mandatory:
        raise ExecutionError('Cannot find setting {0}'.format(name))
    return setting


def get_project_root():
    return get_setting('project_root')


class ThreadProgress():

    """
    Animates an indicator, [=   ], in the status area while a thread runs
    Conveniently grabbed and modified from the Package Control source (MIT
    licensed) but not considered a "substantial portion".
    """

    def __init__(self, thread, message, success_message):
        self.thread = thread
        self.message = message
        self.success_message = success_message
        self.addend = 1
        self.size = 8
        sublime.set_timeout(lambda: self.run(0), 100)

    def run(self, i):
        if not self.thread.is_alive():
            if hasattr(self.thread, 'result') and not self.thread.result:
                sublime.status_message('')
                return
            sublime.status_message(self.success_message)
            return

        before = i % self.size
        after = (self.size - 1) - before

        sublime.status_message('%s [%s=%s]' %
                              (self.message, ' ' * before, ' ' * after))

        if not after:
            self.addend = -1
        if not before:
            self.addend = 1
        i += self.addend
        sublime.set_timeout(lambda: self.run(i), 100)