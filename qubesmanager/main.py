#!/usr/bin/python2
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2010  Joanna Rutkowska <joanna@invisiblethingslab.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
#

import sys
import os
import os.path
import signal
import fcntl
import errno
from PyQt4.QtCore import *
from PyQt4.QtGui import *
from PyQt4.QtDBus import QDBus,QDBusVariant, QDBusMessage
from PyQt4.QtDBus import QDBusConnection
from PyQt4.QtDBus import QDBusInterface,QDBusAbstractAdaptor

from qubes.qubes import QubesVmCollection
from qubes.qubes import QubesException
from qubes.qubes import system_path,vm_files
from qubes.qubes import QubesVmLabels
from qubes.qubes import dry_run
from qubes.qubes import QubesDaemonPidfile
from qubes.qubes import QubesHost
import table_widgets
from block import QubesBlockDevicesManager
from table_widgets import VmTypeWidget, VmLabelWidget, VmNameItem, \
    VmInfoWidget, VmTemplateItem, VmNetvmItem, VmUsageBarWidget, ChartWidget, \
    VmSizeOnDiskItem, VmInternalItem, VmIPItem, VmIncludeInBackupsItem, \
    VmLastBackupItem

try:
    from qubes.qubes import QubesHVm
except ImportError:
    pass
from qubes import qubes
from qubes import qubesutils

import qubesmanager.resources_rc
import ui_newappvmdlg
from ui_mainwindow import *
from create_new_vm import NewVmDlg
from settings import VMSettingsWindow
from restore import RestoreVMsWindow
from backup import BackupVMsWindow
from global_settings import GlobalSettingsWindow
from log_dialog import LogDialog
from thread_monitor import *

from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent

import subprocess
import time
from datetime import datetime,timedelta
qubes_clipboard_info_file = "/var/run/qubes/qubes-clipboard.bin.source"

update_suggestion_interval = 14 # 14 days

dbus_object_path = '/org/qubesos/QubesManager'
dbus_interface = 'org.qubesos.QubesManager'
system_bus = None
session_bus = None


class QMVmState:
    ErrorMsg = 1
    AudioRecAvailable = 2
    AudioRecAllowed = 3

class QubesManagerFileWatcher(ProcessEvent):
    def __init__ (self, update_func):
        self.update_func = update_func

    def process_IN_MODIFY (self, event):
        if event.path == system_path["qubes_store_filename"]:
            self.update_func()

    def process_IN_MOVED_TO (self, event):
        if event.pathname == system_path["qubes_store_filename"]:
            self.update_func()

    def process_IN_CLOSE_WRITE (self, event):
        if event.path == qubes_clipboard_info_file:
            src_info_file = open (qubes_clipboard_info_file, 'r')
            src_vmname = src_info_file.readline().strip('\n')
            if src_vmname == "":
                trayIcon.showMessage(
                    "Qubes Clipboard has been copied to the VM and wiped.<i/>\n"
                    "<small>Trigger a paste operation (e.g. Ctrl-v) to insert it into an application.</small>",
                    msecs=3000)
            else:
                trayIcon.showMessage(
                    "Qubes Clipboard fetched from VM: <b>'{0}'</b>\n"
                    "<small>Press Ctrl-Shift-v to copy this clipboard onto dest VM's clipboard.</small>".format(src_vmname),
                    msecs=3000)
            src_info_file.close()
    def process_IN_CREATE(self, event):
        if event.name == os.path.basename(qubes_clipboard_info_file):
            event.path = qubes_clipboard_info_file
            self.process_IN_CLOSE_WRITE(event)
            wm.add_watch(qubes_clipboard_info_file, EventsCodes.OP_FLAGS.get('IN_CLOSE_WRITE'))
        elif event.name == os.path.basename(table_widgets\
                .qubes_dom0_updates_stat_file):
            trayIcon.showMessage("Qubes dom0 updates available.", msecs=0)



class VmRowInTable(object):
    cpu_graph_hue = 210
    mem_graph_hue = 120
    prefmem_graph_hue = 70

    def __init__(self, vm, row_no, table, block_manager):
        self.vm = vm
        self.row_no = row_no

        table_widgets.row_height = VmManagerWindow.row_height
        table.setRowHeight (row_no, VmManagerWindow.row_height)

        self.type_widget = VmTypeWidget(vm)
        table.setCellWidget(row_no, VmManagerWindow.columns_indices['Type'], self.type_widget)
        table.setItem(row_no, VmManagerWindow.columns_indices['Type'], self.type_widget.tableItem)

        self.label_widget = VmLabelWidget(vm)
        table.setCellWidget(row_no, VmManagerWindow.columns_indices['Label'], self.label_widget)
        table.setItem(row_no, VmManagerWindow.columns_indices['Label'], self.label_widget.tableItem)

        self.name_widget = VmNameItem(vm)
        table.setItem(row_no, VmManagerWindow.columns_indices['Name'], self.name_widget)

        self.info_widget = VmInfoWidget(vm)
        table.setCellWidget(row_no, VmManagerWindow.columns_indices['State'], self.info_widget)
        table.setItem(row_no, VmManagerWindow.columns_indices['State'], self.info_widget.tableItem)

        self.template_widget = VmTemplateItem(vm)
        table.setItem(row_no,  VmManagerWindow.columns_indices['Template'], self.template_widget)

        self.netvm_widget = VmNetvmItem(vm)
        table.setItem(row_no,  VmManagerWindow.columns_indices['NetVM'], self.netvm_widget)

        self.cpu_usage_widget = VmUsageBarWidget(0, 100, "%v %",
                            lambda vm, val: val if vm.last_running else 0, vm, 0, self.cpu_graph_hue)
        table.setCellWidget(row_no,  VmManagerWindow.columns_indices['CPU'], self.cpu_usage_widget)
        table.setItem(row_no,  VmManagerWindow.columns_indices['CPU'], self.cpu_usage_widget.tableItem)

        self.load_widget = ChartWidget(vm, lambda vm, val: val if vm.last_running else 0, self.cpu_graph_hue, 0 )
        table.setCellWidget(row_no,  VmManagerWindow.columns_indices['CPU Graph'], self.load_widget)
        table.setItem(row_no,  VmManagerWindow.columns_indices['CPU Graph'], self.load_widget.tableItem)

        self.mem_usage_widget = VmUsageBarWidget(0, qubes_host.memory_total/1024, "%v MB",
                            lambda vm, val: vm.get_mem()/1024, vm, 0, self.mem_graph_hue)
        table.setCellWidget(row_no,  VmManagerWindow.columns_indices['MEM'], self.mem_usage_widget)
        table.setItem(row_no,  VmManagerWindow.columns_indices['MEM'], self.mem_usage_widget.tableItem)

        self.mem_widget = ChartWidget(vm, lambda vm, val: vm.get_mem()*100/qubes_host.memory_total, self.mem_graph_hue, 0)
        table.setCellWidget(row_no,  VmManagerWindow.columns_indices['MEM Graph'], self.mem_widget)
        table.setItem(row_no,  VmManagerWindow.columns_indices['MEM Graph'], self.mem_widget.tableItem)

        self.prefmem_usage_widget = VmUsageBarWidget(0, qubes_host.memory_total/1024, "%v MB",
                            lambda vm, val: vm.last_prefmem/1024, vm, 0,
                            self.prefmem_graph_hue)
        table.setCellWidget(row_no,
                            VmManagerWindow.columns_indices['Req MEM'],
                            self.prefmem_usage_widget)
        table.setItem(row_no,
                      VmManagerWindow.columns_indices['Req MEM'],
                      self.prefmem_usage_widget.tableItem)

        self.prefmem_widget = \
            ChartWidget(vm,
                        lambda vm, val: vm .last_prefmem*100/qubes_host\
            .memory_total,  self.prefmem_graph_hue, 0)
        table.setCellWidget(row_no,
                            VmManagerWindow.columns_indices['Req MEM Graph'],
                            self.prefmem_widget)
        table.setItem(row_no,
                      VmManagerWindow.columns_indices['Req MEM Graph'],
                      self.prefmem_widget.tableItem)


        self.size_widget = VmSizeOnDiskItem(vm)
        table.setItem(row_no,  VmManagerWindow.columns_indices['Size'], self.size_widget)

        self.internal_widget = VmInternalItem(vm)
        table.setItem(row_no, VmManagerWindow.columns_indices['Internal'], self.internal_widget)

        self.ip_widget = VmIPItem(vm)
        table.setItem(row_no, VmManagerWindow.columns_indices['IP'], self.ip_widget)

        self.include_in_backups_widget = VmIncludeInBackupsItem(vm)
        table.setItem(row_no, VmManagerWindow.columns_indices[
            'Backups'], self.include_in_backups_widget)

        self.last_backup_widget = VmLastBackupItem(vm)
        table.setItem(row_no, VmManagerWindow.columns_indices[
            'Last backup'], self.last_backup_widget)

    def update(self, blk_visible = None, cpu_load = None, update_size_on_disk = False, rec_visible = None):
        self.info_widget.update_vm_state(self.vm, blk_visible, rec_visible)
        if cpu_load is not None:
            self.cpu_usage_widget.update_load(self.vm, cpu_load)
            self.mem_usage_widget.update_load(self.vm, None)
            self.load_widget.update_load(self.vm, cpu_load)
            self.mem_widget.update_load(self.vm, None)
            self.prefmem_widget.update_load(self.vm, None)
            self.prefmem_usage_widget.update_load(self.vm, None)
        if update_size_on_disk == True:
            self.size_widget.update()




vm_shutdown_timeout = 20000 # in msec

class VmShutdownMonitor(QObject):
    def __init__(self, vm, shutdown_time = vm_shutdown_timeout):
        self.vm = vm
        self.shutdown_time = shutdown_time

    def check_if_vm_has_shutdown(self):
        vm = self.vm
        vm_start_time = vm.get_start_time()
        if not vm.is_running() or (vm_start_time and vm_start_time >= datetime.now() - timedelta(0,self.shutdown_time/1000)):
            return

        reply = QMessageBox.question(None, "VM Shutdown",
                                     "The VM <b>'{0}'</b> hasn't shutdown within the last {1} seconds, do you want to kill it?<br>".format(vm.name, self.shutdown_time/1000),
                                     "Kill it!", "Wait another {0} seconds...".format(self.shutdown_time/1000))
        if reply == 0:
            vm.force_shutdown()
        else:
            QTimer.singleShot (self.shutdown_time, self.check_if_vm_has_shutdown)


class VmManagerWindow(Ui_VmManagerWindow, QMainWindow):
    row_height = 30
    column_width = 200
    min_visible_rows = 10
    update_interval = 1000 # in msec
    show_inactive_vms = True
    show_internal_vms = False
    # suppress saving settings while initializing widgets
    settings_loaded = False
    columns_indices = { "Type": 0,
                        "Label": 1,
                        "Name": 2,
                        "State": 3,
                        "Template": 4,
                        "NetVM": 5,
                        "CPU": 6,
                        "CPU Graph": 7,
                        "MEM": 8,
                        "MEM Graph": 9,
                        "Req MEM": 10,
                        "Req MEM Graph": 11,
                        "Size": 12,
                        "Internal": 13,
                        "IP": 14,
                        "Backups": 15,
                        "Last backup": 16,
    }

    def __init__(self, qvm_collection, blk_manager, parent=None):
        super(VmManagerWindow, self).__init__()
        self.setupUi(self)
        self.toolbar = self.toolBar

        self.manager_settings = QSettings()

        self.qubes_watch = qubesutils.QubesWatch()
        self.qvm_collection = qvm_collection
        self.meminfo_changed = {}
        self.blk_manager = blk_manager
        self.blk_manager.tray_message_func = trayIcon.showMessage
        self.qubes_watch.setup_block_watch(self.blk_manager.block_devs_event)
        self.qubes_watch.setup_meminfo_watch(self.meminfo_update_event)
        self.blk_watch_thread = threading.Thread(target=self.qubes_watch.watch_loop)
        self.blk_watch_thread.daemon = True
        self.blk_watch_thread.start()

        self.connect(self.table, SIGNAL("itemSelectionChanged()"), self.table_selection_changed)

        self.table.setColumnWidth(0, self.column_width)

        self.sort_by_column = "Type"
        self.sort_order = Qt.AscendingOrder

        self.screen_number = -1
        self.screen_changed = False

        self.running_vms_count = 0
        self.internal_vms_count = 0

        self.vm_errors = {}
        self.vm_rec = {}

        self.frame_width = 0
        self.frame_height = 0

        self.move(self.x(), 0)

        self.columns_actions = {}
        self.columns_actions[ self.columns_indices["Type"] ] = self.action_vm_type
        self.columns_actions[ self.columns_indices["Label"] ] = self.action_label
        self.columns_actions[ self.columns_indices["Name"] ] = self.action_name
        self.columns_actions[ self.columns_indices["State"] ] = self.action_state
        self.columns_actions[ self.columns_indices["Template"] ] = self.action_template
        self.columns_actions[ self.columns_indices["NetVM"] ] = self.action_netvm
        self.columns_actions[ self.columns_indices["CPU"] ] = self.action_cpu
        self.columns_actions[ self.columns_indices["CPU Graph"] ] = self.action_cpu_graph
        self.columns_actions[ self.columns_indices["MEM"] ] = self.action_mem
        self.columns_actions[ self.columns_indices["MEM Graph"] ] = self.action_mem_graph
        self.columns_actions[ self.columns_indices["Req MEM"] ] = self\
            .action_prefmem
        self.columns_actions[ self.columns_indices["Req MEM Graph"] ] = self\
            .action_prefmem_graph
        self.columns_actions[ self.columns_indices["Size"] ] = self.action_size_on_disk
        self.columns_actions[ self.columns_indices["Internal"] ] = self.action_internal
        self.columns_actions[ self.columns_indices["IP"] ] = self\
            .action_ip
        self.columns_actions[ self.columns_indices["Backups"] ] = self\
            .action_backups
        self.columns_actions[ self.columns_indices["Last backup"] ] = self\
            .action_last_backup

        self.visible_columns_count = len(self.columns_indices)
        self.table.setColumnHidden( self.columns_indices["NetVM"], True)
        self.action_netvm.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["CPU Graph"], True)
        self.action_cpu_graph.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["MEM Graph"], True)
        self.action_mem_graph.setChecked(False)
        self.action_prefmem.setChecked(False)
        self.action_prefmem_graph.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["Size"], True)
        self.action_size_on_disk.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["Internal"], True)
        self.action_internal.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["IP"], True)
        self.action_ip.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["Backups"], True)
        self.action_backups.setChecked(False)
        self.table.setColumnHidden( self.columns_indices["Last backup"], True)
        self.action_last_backup.setChecked(False)

        self.table.setColumnWidth(self.columns_indices["State"], 80)
        self.table.setColumnWidth(self.columns_indices["Name"], 150)
        self.table.setColumnWidth(self.columns_indices["Label"], 40)
        self.table.setColumnWidth(self.columns_indices["Type"], 40)
        self.table.setColumnWidth(self.columns_indices["Size"], 100)
        self.table.setColumnWidth(self.columns_indices["Internal"], 60)
        self.table.setColumnWidth(self.columns_indices["IP"], 100)
        self.table.setColumnWidth(self.columns_indices["Backups"], 60)
        self.table.setColumnWidth(self.columns_indices["Last backup"], 90)

        self.table.horizontalHeader().setResizeMode(QHeaderView.Fixed)

        self.table.sortItems(self.columns_indices[self.sort_by_column], self.sort_order)

        self.context_menu = QMenu(self)

        self.context_menu.addAction(self.action_settings)
        self.context_menu.addAction(self.action_editfwrules)
        self.context_menu.addAction(self.action_appmenus)
        self.context_menu.addAction(self.action_set_keyboard_layout)
        self.context_menu.addMenu(self.blk_menu)
        self.context_menu.addAction(self.action_toggle_audio_input)
        self.context_menu.addSeparator()

        self.context_menu.addAction(self.action_updatevm)
        self.context_menu.addAction(self.action_run_command_in_vm)
        self.context_menu.addAction(self.action_resumevm)
        self.context_menu.addAction(self.action_startvm_tools_install)
        self.context_menu.addAction(self.action_pausevm)
        self.context_menu.addAction(self.action_shutdownvm)
        self.context_menu.addAction(self.action_killvm)
        self.context_menu.addSeparator()

        self.context_menu.addAction(self.action_clonevm)
        self.context_menu.addAction(self.action_removevm)
        self.context_menu.addSeparator()

        self.context_menu.addMenu(self.logs_menu)
        self.context_menu.addSeparator()

        self.tools_context_menu = QMenu(self)
        self.tools_context_menu.addAction(self.action_toolbar)
        self.tools_context_menu.addAction(self.action_menubar)

        self.table_selection_changed()

        self.connect(self.table.horizontalHeader(), SIGNAL("sortIndicatorChanged(int, Qt::SortOrder)"), self.sortIndicatorChanged)
        self.connect(self.table, SIGNAL("customContextMenuRequested(const QPoint&)"), self.open_context_menu)
        self.connect(self.menubar, SIGNAL("customContextMenuRequested(const QPoint&)"),
            lambda pos: self.open_tools_context_menu(self.menubar, pos))
        self.connect(self.toolBar, SIGNAL("customContextMenuRequested(const QPoint&)"),
            lambda pos: self.open_tools_context_menu(self.toolBar, pos))
        self.connect(self.blk_menu, SIGNAL("triggered(QAction *)"), self.attach_dettach_device_triggered)
        self.connect(self.logs_menu, SIGNAL("triggered(QAction *)"), self.show_log)

        self.table.setContentsMargins(0,0,0,0)
        self.centralwidget.layout().setContentsMargins(0,0,0,0)
        self.layout().setContentsMargins(0,0,0,0)

        self.connect(self.action_menubar, SIGNAL("toggled(bool)"), self.showhide_menubar)
        self.connect(self.action_toolbar, SIGNAL("toggled(bool)"), self.showhide_toolbar)

        self.register_dbus_watches()

        self.load_manager_settings()

        self.action_showallvms.setChecked(self.show_inactive_vms)
        self.action_showinternalvms.setChecked(self.show_internal_vms)

        self.fill_table()

        self.counter = 0
        self.update_size_on_disk = False
        self.shutdown_monitor = {}
        self.last_measure_results = {}
        self.last_measure_time = time.time()
        QTimer.singleShot (self.update_interval, self.update_table)

        QubesDbusNotifyServerAdaptor(self)

    def load_manager_settings(self):
        # visible columns
        self.manager_settings.beginGroup("columns")
        for col in self.columns_indices.keys():
            col_no = self.columns_indices[col]
            visible = self.manager_settings.value(col, defaultValue=not self.table.isColumnHidden(col_no)).toBool()
            self.columns_actions[col_no].setChecked(visible)
        self.manager_settings.endGroup()
        self.show_inactive_vms = self.manager_settings.value("view/show_inactive_vms", defaultValue=False).toBool()
        self.show_internal_vms = self.manager_settings.value("view/show_internal_vms", defaultValue=False).toBool()
        self.sort_by_column = str(self.manager_settings.value("view/sort_column", defaultValue=self.sort_by_column).toString())
        self.sort_order = Qt.SortOrder(self.manager_settings.value("view/sort_order", defaultValue=self.sort_order).toInt()[0])
        self.table.sortItems(self.columns_indices[self.sort_by_column], self.sort_order)
        if not self.manager_settings.value("view/menubar_visible", defaultValue=True).toBool():
            self.action_menubar.setChecked(False)
        if not self.manager_settings.value("view/toolbar_visible", defaultValue=True).toBool():
            self.action_toolbar.setChecked(False)
        x = self.manager_settings.value('position/x', defaultValue=-1).toInt()[0]
        y = self.manager_settings.value('position/y', defaultValue=-1).toInt()[0]
        if x != -1 or y != -1:
            self.move(x, y)
        self.settings_loaded = True

    def show(self):
        super(VmManagerWindow, self).show()
        self.screen_number = app.desktop().screenNumber(self)

    def set_table_geom_size(self):

        desktop_width = app.desktop().availableGeometry(self).width() - self.frame_width # might be wrong...
        desktop_height = app.desktop().availableGeometry(self).height() - self.frame_height # might be wrong...
        desktop_height -= self.row_height #UGLY! to somehow ommit taskbar...

        W = self.table.horizontalHeader().length() +\
            self.table.verticalScrollBar().width() +\
            2*self.table.frameWidth() +1

        H = self.table.horizontalHeader().height() +\
            2*self.table.frameWidth()

        mainwindow_to_add = 0

        available_space = desktop_height
        if self.menubar.isVisible():
            menubar_height = self.menubar.sizeHint().height() + self.menubar.contentsMargins().top() + self.menubar.contentsMargins().bottom()
            available_space -= menubar_height
            mainwindow_to_add += menubar_height
        if self.toolbar.isVisible():
            toolbar_height = self.toolbar.sizeHint().height() + self.toolbar.contentsMargins().top() + self.toolbar.contentsMargins().bottom()
            available_space -= toolbar_height
            mainwindow_to_add += toolbar_height
        if W >= desktop_width:
            available_space -= self.table.horizontalScrollBar().height()
            H += self.table.horizontalScrollBar().height()
        default_rows = int(available_space/self.row_height)

        if self.show_inactive_vms:
            n = self.table.rowCount()
        else:
            n = self.running_vms_count
        if self.show_internal_vms:
            if self.show_inactive_vms:
                n = self.table.rowCount()
            else:
                n = self.running_vms_count
        elif self.show_inactive_vms:
            n = self.table.rowCount() - self.internal_vms_count
        else:
            n = self.running_vms_count

        if n > default_rows:
            H += default_rows*self.row_height
            self.table.verticalScrollBar().show()
        else:
            H += n*self.row_height
            self.table.verticalScrollBar().hide()
            W -= self.table.verticalScrollBar().width()

        W = min(desktop_width, W)

        self.centralwidget.setFixedHeight(H)

        H += mainwindow_to_add

        self.setMaximumHeight(H)
        self.setMinimumHeight(H)

        self.table.setFixedWidth(W)
        self.centralwidget.setFixedWidth(W)
        # don't change the following two lines to setFixedWidth!
        self.setMaximumWidth(W)
        self.setMinimumWidth(W)


    def moveEvent(self, event):
        super(VmManagerWindow, self).moveEvent(event)
        screen_number = app.desktop().screenNumber(self)
        if self.screen_number != screen_number:
                self.screen_changed = True
                self.screen_number = screen_number
        if self.settings_loaded:
            self.manager_settings.setValue('position/x', self.x())
            self.manager_settings.setValue('position/y', self.y())
            # do not sync for performance reasons

    def get_vms_list(self):
        self.qvm_collection.lock_db_for_reading()
        self.qvm_collection.load()
        self.qvm_collection.unlock_db()

        running_count = 0
        internal_count = 0

        vms_list = [vm for vm in self.qvm_collection.values()]
        for vm in vms_list:
            vm.last_power_state = vm.get_power_state()
            vm.last_running = vm.last_power_state in ["Running", "Transient"]
            vm.last_prefmem = vm.get_prefmem()
            if vm.last_running:
                running_count += 1
            if vm.internal:
                internal_count += 1
            vm.qubes_manager_state = {}
            self.update_audio_rec_info(vm)
            vm.qubes_manager_state[QMVmState.ErrorMsg]= self.vm_errors[vm.qid] if vm.qid in self.vm_errors else None

        self.running_vms_count = running_count
        self.internal_vms_count = internal_count
        return vms_list

    def fill_table(self):
        # save current selection
        row_index = self.table.currentRow()
        selected_qid = -1
        if row_index != -1:
            vm_item = self.table.item(row_index, self.columns_indices["Name"])
            if vm_item:
                selected_qid = vm_item.qid

        self.table.setSortingEnabled(False)
        self.table.clearContents()
        vms_list = self.get_vms_list()
        self.table.setRowCount(len(vms_list))

        vms_in_table = {}

        row_no = 0
        for vm in vms_list:
            #if vm.internal:
            #    continue
            vm_row = VmRowInTable (vm, row_no, self.table, self.blk_manager)
            vms_in_table[vm.qid] = vm_row

            row_no += 1

        self.table.setRowCount(row_no)
        self.vms_list = vms_list
        self.vms_in_table = vms_in_table
        self.reload_table = False
        if selected_qid in vms_in_table.keys():
            self.table.setCurrentItem(self.vms_in_table[selected_qid].name_widget)
        self.table.setSortingEnabled(True)

        self.showhide_vms(True, True)
        self.set_table_geom_size()
        if (not self.show_inactive_vms) or (not self.show_internal_vms):
            self.showhide_vms(self.show_inactive_vms, self.show_internal_vms)
            self.set_table_geom_size()

    def showhide_vms(self, show_inactive, show_internal):
        if show_inactive and show_internal:
            row_no = 0
            while row_no < self.table.rowCount():
                self.table.setRowHidden(row_no, False)
                row_no += 1
        else:
            row_no = 0
            while row_no < self.table.rowCount():
                widget = self.table.cellWidget(row_no, self.columns_indices["State"])
                running = widget.vm.last_running
                internal = widget.vm.internal
                if not (show_inactive or running) or not (show_internal or not internal):
                    self.table.setRowHidden(row_no, True)
                else:
                    self.table.setRowHidden(row_no, False)
                row_no += 1

    def meminfo_update_event(self, domain_id):
        self.meminfo_changed[int(domain_id)] = True

    def mark_table_for_update(self):
        self.reload_table = True

    # When calling update_table() directly, always use out_of_schedule=True!
    def update_table(self, out_of_schedule=False):

        update_devs = self.update_block_devices() or out_of_schedule
        reload_table = self.reload_table

        if manager_window.isVisible():
            some_vms_have_changed_power_state = False
            for vm in self.vms_list:
                state = vm.get_power_state()
                if vm.last_power_state != state:
                    if state == "Running" and \
                            self.vm_errors.get(vm.qid,  "")\
                            .startswith("Error starting VM:"):
                        self.clear_error(vm.qid)
                    prev_running = vm.last_running
                    vm.last_power_state = state
                    vm.last_running = (state in ["Running", "Transient"])
                    self.update_audio_rec_info(vm)
                    if not prev_running and vm.last_running:
                        self.running_vms_count += 1
                        some_vms_have_changed_power_state = True
                        # Clear error state when VM just started
                        self.clear_error(vm.qid)
                    elif prev_running and not vm.last_running:
                        # FIXME: remove when recAllowed state will be preserved
                        if self.vm_rec.has_key(vm.name):
                            self.vm_rec.pop(vm.name)
                        self.running_vms_count -= 1
                        some_vms_have_changed_power_state = True
                else:
                    # pulseaudio agent register itself some time after VM
                    # startup
                    if state == "Running" and not vm.qubes_manager_state[QMVmState.AudioRecAvailable]:
                        self.update_audio_rec_info(vm)
                if self.vm_errors.get(vm.qid, "") == \
                        "Error starting VM: Cannot execute qrexec-daemon!" \
                        and vm.is_qrexec_running():
                    self.clear_error(vm.qid)
                if self.meminfo_changed.get(vm.xid, True):
                    vm.last_prefmem = vm.get_prefmem()
                    self.meminfo_changed[vm.xid] = False

            if self.screen_changed == True:
                reload_table = True
                self.screen_changed = False

            if reload_table:
                self.fill_table()
                update_devs=True

            if (not self.show_inactive_vms) and some_vms_have_changed_power_state:
                self.showhide_vms(True, True)
                self.showhide_vms(False, self.show_internal_vms)
                self.set_table_geom_size()

            if self.sort_by_column == "State" and some_vms_have_changed_power_state:
                self.table.sortItems(self.columns_indices[self.sort_by_column], self.sort_order)

            blk_visible = None
            rows_with_blk = None
            if update_devs == True:
                rows_with_blk = []
                self.blk_manager.blk_lock.acquire()
                for d in self.blk_manager.attached_devs:
                    rows_with_blk.append( self.blk_manager.attached_devs[d]['attached_to']['vm'])
                self.blk_manager.blk_lock.release()

            if (not self.table.isColumnHidden(self.columns_indices['Size']) ) and self.counter % 60 == 0 or out_of_schedule:
                self.update_size_on_disk = True

            if self.counter % 3 == 0 or out_of_schedule:
                (self.last_measure_time, self.last_measure_results) = \
                    qubes_host.measure_cpu_usage(self.last_measure_results,
                    self.last_measure_time)

                for vm_row in self.vms_in_table.values():
                    cur_cpu_load = None
                    if vm_row.vm.get_xid() in self.last_measure_results:
                        cur_cpu_load = self.last_measure_results[vm_row.vm.xid]['cpu_usage']
                    else:
                        cur_cpu_load = 0

                    if rows_with_blk != None:
                        if vm_row.vm.name in rows_with_blk:
                            blk_visible = True
                        else:
                            blk_visible = False

                    vm_row.update(blk_visible=blk_visible, cpu_load = cur_cpu_load, update_size_on_disk = self.update_size_on_disk, rec_visible = self.vm_rec.get(vm_row.vm.name, False))

            else:
                for vm_row in self.vms_in_table.values():
                    if rows_with_blk != None:
                        if vm_row.vm.name in rows_with_blk:
                            blk_visible = True
                        else:
                            blk_visible = False

                    vm_row.update(blk_visible=blk_visible, update_size_on_disk = self.update_size_on_disk, rec_visible = self.vm_rec.get(vm_row.vm.name, False))

            if self.sort_by_column in ["CPU", "CPU Graph", "MEM", "MEM Graph", "State", "Size", "Internal" ]:
                # "State": needed to sort after reload (fill_table sorts items with setSortingEnabled, but by that time the widgets values are not correct yet).
                self.table.sortItems(self.columns_indices[self.sort_by_column], self.sort_order)

            self.table_selection_changed()

        self.update_size_on_disk = False
        if not out_of_schedule:
            self.counter += 1
            QTimer.singleShot (self.update_interval, self.update_table)


    def update_block_devices(self):
        res, msg = self.blk_manager.check_for_updates()
        if msg != None and len(msg) > 0:
            trayIcon.showMessage ('\n'.join(msg), msecs=5000)
        return res

    @pyqtSlot(bool, str)
    def recAllowedChanged(self, state, vmname):
        self.vm_rec[str(vmname)] = bool(state)

    def register_dbus_watches(self):
        global session_bus

        if not session_bus:
            session_bus = QDBusConnection.sessionBus()

        if not session_bus.connect(QString(), # service
                QString(), # path
                QString("org.QubesOS.Audio"), # interface
                QString("RecAllowedChanged"), # name
                self.recAllowedChanged): # slot
            print session_bus.lastError().message()

    def sortIndicatorChanged(self, column, order):
        self.sort_by_column = [name for name in self.columns_indices.keys() if self.columns_indices[name] == column][0]
        self.sort_order = order
        if self.settings_loaded:
            self.manager_settings.setValue('view/sort_column', self.sort_by_column)
            self.manager_settings.setValue('view/sort_order', self.sort_order)
            self.manager_settings.sync()

    def table_selection_changed (self):

        vm = self.get_selected_vm()

        if vm != None:
            # Update available actions:
            self.action_settings.setEnabled(vm.qid != 0)
            self.action_removevm.setEnabled(not vm.installed_by_rpm and not (vm.last_running))
            self.action_clonevm.setEnabled(not (vm.last_running) and not vm.is_netvm())
            self.action_resumevm.setEnabled(not vm.last_running)
            try:
                self.action_startvm_tools_install.setVisible(isinstance(vm,
                                                                    QubesHVm))
            except NameError:
                # ignore non existing QubesHVm
                pass
            self.action_startvm_tools_install.setEnabled(not vm.last_running)
            self.action_pausevm.setEnabled(vm.last_running and vm.qid != 0)
            self.action_shutdownvm.setEnabled(vm.last_running and vm.qid != 0)
            self.action_killvm.setEnabled((vm.last_running or vm.last_power_state == "Paused") and vm.qid != 0)
            self.action_appmenus.setEnabled(not vm.is_netvm())
            self.action_editfwrules.setEnabled(vm.is_networked() and not (vm.is_netvm() and not vm.is_proxyvm()))
            self.action_updatevm.setEnabled(vm.is_updateable() or vm.qid == 0)
            self.action_toggle_audio_input.setEnabled(vm.qubes_manager_state[QMVmState.AudioRecAvailable])
            self.action_run_command_in_vm.setEnabled(not vm.last_power_state == "Paused" and vm.qid != 0)
            self.action_set_keyboard_layout.setEnabled(vm.qid != 0 and vm.last_running)
        else:
            self.action_settings.setEnabled(False)
            self.action_removevm.setEnabled(False)
            self.action_startvm_tools_install.setVisible(False)
            self.action_startvm_tools_install.setEnabled(False)
            self.action_clonevm.setEnabled(False)
            self.action_resumevm.setEnabled(False)
            self.action_pausevm.setEnabled(False)
            self.action_shutdownvm.setEnabled(False)
            self.action_killvm.setEnabled(False)
            self.action_appmenus.setEnabled(False)
            self.action_editfwrules.setEnabled(False)
            self.action_updatevm.setEnabled(False)
            self.action_toggle_audio_input.setEnabled(False)
            self.action_run_command_in_vm.setEnabled(False)
            self.action_set_keyboard_layout.setEnabled(False)



    def closeEvent (self, event):
        if event.spontaneous(): # There is something borked in Qt, as the logic here is inverted on X11
            self.hide()
            event.ignore()


    def set_error(self, qid, message):
        for vm in self.vms_list:
            if vm.qid == qid:
                vm.qubes_manager_state[QMVmState.ErrorMsg] = message
        # Store error in separate dict to make it immune to VM list reload
        self.vm_errors[qid] = str(message)

    def clear_error(self, qid):
        self.vm_errors.pop(qid, None)
        for vm in self.vms_list:
            if vm.qid == qid:
                vm.qubes_manager_state[QMVmState.ErrorMsg] = None

    def clear_error_exact(self, qid, message):
        for vm in self.vms_list:
            if vm.qid == qid:
                if vm.qubes_manager_state[QMVmState.ErrorMsg] == message:
                    vm.qubes_manager_state[QMVmState.ErrorMsg] = None
                    self.vm_errors.pop(qid, None)

    @pyqtSlot(name='on_action_createvm_triggered')
    def action_createvm_triggered(self):
        dialog = NewVmDlg(app, self.qvm_collection, trayIcon)
        dialog.exec_()


    def get_selected_vm(self):
        #vm selection relies on the VmInfo widget's value used for sorting by VM name
        row_index = self.table.currentRow()
        if row_index != -1:
            vm_item = self.table.item(row_index, self.columns_indices["Name"])
            # here is possible race with update_table timer so check if really got the item
            if vm_item is None:
                return None
            qid = vm_item.qid
            assert self.vms_in_table[qid] is not None
            vm = self.vms_in_table[qid].vm
            return vm
        else:
            return None

    @pyqtSlot(name='on_action_removevm_triggered')
    def action_removevm_triggered(self):

        vm = self.get_selected_vm()
        assert not vm.is_running()
        assert not vm.installed_by_rpm

        self.qvm_collection.lock_db_for_reading()
        self.qvm_collection.load()
        self.qvm_collection.unlock_db()
        vm = self.qvm_collection[vm.qid]

        if vm.is_template():
            dependent_vms = self.qvm_collection.get_vms_based_on(vm.qid)
            if len(dependent_vms) > 0:
                QMessageBox.warning (None, "Warning!",
                                     "This Template VM cannot be removed, because there is at least one AppVM that is based on it.<br>"
                                     "<small>If you want to remove this Template VM and all the AppVMs based on it,"
                                     "you should first remove each individual AppVM that uses this template.</small>")

                return

        reply = QMessageBox.question(None, "VM Removal Confirmation",
                                     "Are you sure you want to remove the VM <b>'{0}'</b>?<br>"
                                     "<small>All data on this VM's private storage will be lost!</small>".format(vm.name),
                                     QMessageBox.Yes | QMessageBox.Cancel)


        if reply == QMessageBox.Yes:

            thread_monitor = ThreadMonitor()
            thread = threading.Thread (target=self.do_remove_vm, args=(vm, thread_monitor))
            thread.daemon = True
            thread.start()

            progress = QProgressDialog ("Removing VM: <b>{0}</b>...".format(vm.name), "", 0, 0)
            progress.setCancelButton(None)
            progress.setModal(True)
            progress.show()

            while not thread_monitor.is_finished():
                app.processEvents()
                time.sleep (0.1)

            progress.hide()

            if thread_monitor.success:
                trayIcon.showMessage ("VM '{0}' has been removed.".format(vm.name), msecs=3000)
            else:
                QMessageBox.warning (None, "Error removing VM!", "ERROR: {0}".format(thread_monitor.error_msg))

    def do_remove_vm (self, vm, thread_monitor):
        try:
            self.qvm_collection.lock_db_for_writing()
            self.qvm_collection.load()
            vm = self.qvm_collection[vm.qid]

            #TODO: the following two conditions should really be checked by qvm_collection.pop() overload...
            if vm.is_template() and self.qvm_collection.default_template_qid == vm.qid:
                self.qvm_collection.default_template_qid = None
            if vm.is_netvm() and self.qvm_collection.default_netvm_qid == vm.qid:
                self.qvm_collection.default_netvm_qid = None

            self.qvm_collection.pop(vm.qid)
            self.qvm_collection.save()
            vm.remove_from_disk()
        except Exception as ex:
            thread_monitor.set_error_msg (str(ex))
        finally:
            self.qvm_collection.unlock_db()

        thread_monitor.set_finished()

    @pyqtSlot(name='on_action_clonevm_triggered')
    def action_clonevm_triggered(self):
        vm = self.get_selected_vm()

        name_number = 1
        name_format = vm.name + '-clone-%d'
        while self.qvm_collection.get_vm_by_name(name_format % name_number):
            name_number += 1

        (clone_name, ok) = QInputDialog.getText(self, 'Qubes clone VM',
                'Enter name for VM <b>'+vm.name+'</b> clone:', text=name_format % name_number)
        if not ok or clone_name == "":
            return

        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.do_clone_vm, args=(vm, str(clone_name), thread_monitor))
        thread.daemon = True
        thread.start()

        progress = QProgressDialog ("Cloning VM <b>{0}</b> to <b>{1}</b>...".format(vm.name, clone_name), "", 0, 0)
        progress.setCancelButton(None)
        progress.setModal(True)
        progress.show()

        while not thread_monitor.is_finished():
            app.processEvents()
            time.sleep (0.2)

        progress.hide()

        if not thread_monitor.success:
            QMessageBox.warning (None, "Error while cloning VM", "Exception while cloning:<br>{0}".format(thread_monitor.error_msg))


    def do_clone_vm(self, vm, dst_name, thread_monitor):
        dst_vm = None
        try:
            self.qvm_collection.lock_db_for_writing()
            self.qvm_collection.load()
            src_vm = self.qvm_collection[vm.qid]

            dst_vm = self.qvm_collection.add_new_vm(src_vm.__class__.__name__,
                    name=dst_name, template=src_vm.template,
                    installed_by_rpm=False)

            dst_vm.clone_attrs(src_vm)
            dst_vm.clone_disk_files (src_vm=src_vm, verbose=False)
            self.qvm_collection.save()
            self.qvm_collection.unlock_db()
        except Exception as ex:
            if dst_vm:
                self.qvm_collection.pop(dst_vm.qid)
                dst_vm.remove_from_disk()
            self.qvm_collection.unlock_db()
            thread_monitor.set_error_msg(str(ex))
        thread_monitor.set_finished()

    @pyqtSlot(name='on_action_resumevm_triggered')
    def action_resumevm_triggered(self):
        vm = self.get_selected_vm()
        assert not vm.is_running()

        if vm.is_paused():
            try:
                vm.unpause()
            except Exception as ex:
                QMessageBox.warning (None, "Error unpausing VM!", "ERROR: {0}".format(ex))
            return

        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.do_start_vm, args=(vm, thread_monitor))
        thread.daemon = True
        thread.start()

        trayIcon.showMessage ("Starting '{0}'...".format(vm.name), msecs=3000)

        while not thread_monitor.is_finished():
            app.processEvents()
            time.sleep (0.1)

        if thread_monitor.success:
            trayIcon.showMessage ("VM '{0}' has been started.".format(vm.name), msecs=3000)
        else:
            trayIcon.showMessage ("Error starting VM <b>'{0}'</b>: {1}".format(vm.name, thread_monitor.error_msg ), msecs=3000)
            self.set_error(vm.qid, "Error starting VM: %s" % thread_monitor.error_msg)

    def do_start_vm(self, vm, thread_monitor):
        try:
            vm.verify_files()
            xid = vm.start()
        except Exception as ex:
            thread_monitor.set_error_msg(str(ex))
            thread_monitor.set_finished()
            return

        thread_monitor.set_finished()

    @pyqtSlot(name='on_action_startvm_tools_install_triggered')
    def action_startvm_tools_install_triggered(self):
        vm = self.get_selected_vm()
        assert not vm.is_running()

        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.do_start_vm_tools_install,
                                   args=(vm, thread_monitor))
        thread.daemon = True
        thread.start()

        trayIcon.showMessage ("Starting '{0}'...".format(vm.name), msecs=3000)

        while not thread_monitor.is_finished():
            app.processEvents()
            time.sleep (0.1)

        if thread_monitor.success:
            trayIcon.showMessage ("VM '{0}' has been started. Start Qubes "
                                  "Tools installation from attached CD"
                                  .format(vm.name), msecs=3000)
        else:
            trayIcon.showMessage ("Error starting VM <b>'{0}'</b>: {1}".format(vm.name, thread_monitor.error_msg ), msecs=3000)
            self.set_error(vm.qid, "Error starting VM: %s" % thread_monitor.error_msg)

    def do_start_vm_tools_install(self, vm, thread_monitor):
        prev_drive = vm.drive
        try:
            vm.verify_files()
            vm.drive = 'cdrom:dom0:/usr/lib/qubes/qubes-windows-tools.iso'
            xid = vm.start()
        except Exception as ex:
            thread_monitor.set_error_msg(str(ex))
            thread_monitor.set_finished()
            return
        finally:
            vm.drive = prev_drive

        thread_monitor.set_finished()

    @pyqtSlot(name='on_action_pausevm_triggered')
    def action_pausevm_triggered(self):
        vm = self.get_selected_vm()
        assert vm.is_running()
        try:
            vm.pause()
        except Exception as ex:
            QMessageBox.warning (None, "Error pausing VM!", "ERROR: {0}".format(ex))
            return

    @pyqtSlot(name='on_action_shutdownvm_triggered')
    def action_shutdownvm_triggered(self):
        vm = self.get_selected_vm()
        assert vm.is_running()

        self.blk_manager.check_if_serves_as_backend(vm)

        reply = QMessageBox.question(None, "VM Shutdown Confirmation",
                                     "Are you sure you want to power down the VM <b>'{0}'</b>?<br>"
                                     "<small>This will shutdown all the running applications within this VM.</small>".format(vm.name),
                                     QMessageBox.Yes | QMessageBox.Cancel)

        app.processEvents()

        if reply == QMessageBox.Yes:
            self.shutdown_vm(vm)


    def shutdown_vm(self, vm, shutdown_time = vm_shutdown_timeout):
        try:
            vm.shutdown()
        except Exception as ex:
            QMessageBox.warning (None, "Error shutting down VM!", "ERROR: {0}".format(ex))
            return

        trayIcon.showMessage ("VM '{0}' is shutting down...".format(vm.name), msecs=3000)

        self.shutdown_monitor[vm.qid] = VmShutdownMonitor (vm, shutdown_time)
        QTimer.singleShot (shutdown_time, self.shutdown_monitor[vm.qid].check_if_vm_has_shutdown)


    @pyqtSlot(name='on_action_killvm_triggered')
    def action_killvm_triggered(self):
        vm = self.get_selected_vm()
        assert vm.is_running() or vm.is_paused()

        reply = QMessageBox.question(None, "VM Kill Confirmation",
                                     "Are you sure you want to kill the VM <b>'{0}'</b>?<br>"
                                     "<small>This will end <b>(not shutdown!)</b> all the running applications within this VM.</small>".format(vm.name),
                                     QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)

        app.processEvents()

        if reply == QMessageBox.Yes:
            try:
                vm.force_shutdown()
            except Exception as ex:
                QMessageBox.critical (None, "Error while killing VM!", "<b>An exception ocurred while killing {0}.</b><br>ERROR: {1}".format(vm.name, ex))
                return

            trayIcon.showMessage ("VM '{0}' killed!".format(vm.name), msecs=3000)



    @pyqtSlot(name='on_action_settings_triggered')
    def action_settings_triggered(self):
        vm = self.get_selected_vm()
        settings_window = VMSettingsWindow(vm, app, self.qvm_collection, "basic")
        settings_window.exec_()


    @pyqtSlot(name='on_action_appmenus_triggered')
    def action_appmenus_triggered(self):
        vm = self.get_selected_vm()
        settings_window = VMSettingsWindow(vm, app, self.qvm_collection, "applications")
        settings_window.exec_()

    def update_audio_rec_info(self, vm):
        vm.qubes_manager_state[QMVmState.AudioRecAvailable] = (
                session_bus.interface().isServiceRegistered('org.QubesOS.Audio.%s' % vm.name).value())
        if vm.qubes_manager_state[QMVmState.AudioRecAvailable]:
            vm.qubes_manager_state[QMVmState.AudioRecAllowed] = self.get_audio_rec_allowed(vm.name)
        else:
            vm.qubes_manager_state[QMVmState.AudioRecAllowed] = False

    def get_audio_rec_allowed(self, vmname):
        properties = QDBusInterface('org.QubesOS.Audio.%s' % vmname,
                                '/org/qubesos/audio', 'org.freedesktop.DBus.Properties', session_bus)

        current_audio = properties.call('Get', 'org.QubesOS.Audio', 'RecAllowed')
        if current_audio.type() == current_audio.ReplyMessage:
            value = current_audio.arguments()[0].toPyObject().toBool()
            return bool(value)
        return False

    @pyqtSlot(name='on_action_toggle_audio_input_triggered')
    def action_toggle_audio_input_triggered(self):
        vm = self.get_selected_vm()
        properties = QDBusInterface('org.QubesOS.Audio.%s' % vm.name,
                                '/org/qubesos/audio', 'org.freedesktop.DBus.Properties', session_bus)
        properties.call('Set', 'org.QubesOS.Audio', 'RecAllowed',
                QDBusVariant(not self.get_audio_rec_allowed(vm.name)))
        # icon will be updated based on dbus signal

    @pyqtSlot(name='on_action_updatevm_triggered')
    def action_updatevm_triggered(self):
        vm = self.get_selected_vm()

        if not vm.is_running():
            reply = QMessageBox.question(None, "VM Update Confirmation",
                    "<b>{0}</b><br>The VM has to be running to be updated.<br>Do you want to start it?<br>".format(vm.name),
                    QMessageBox.Yes | QMessageBox.Cancel)
            if reply != QMessageBox.Yes:
                return
            trayIcon.showMessage ("Starting '{0}'...".format(vm.name), msecs=3000)

        app.processEvents()

        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.do_update_vm, args=(vm, thread_monitor))
        thread.daemon = True
        thread.start()

        progress = QProgressDialog ("<b>{0}</b><br>Please wait for the updater to launch...".format(vm.name), "", 0, 0)
        progress.setCancelButton(None)
        progress.setModal(True)
        progress.show()

        while not thread_monitor.is_finished():
            app.processEvents()
            time.sleep (0.2)

        progress.hide()

        if vm.qid != 0:
            if not thread_monitor.success:
                QMessageBox.warning (None, "Error VM update!", "ERROR: {0}".format(thread_monitor.error_msg))

    def do_update_vm(self, vm, thread_monitor):
        try:
            if vm.qid == 0:
                subprocess.check_call (["/usr/bin/qubes-dom0-update", "--clean", "--gui"])
            else:
                vm_run_common_args = {
                    'verbose': False,
                    'autostart': True,
                    'notify_function': lambda lvl, msg: trayIcon\
                    .showMessage(msg, msecs=3000) }
                vm.run("yum clean expire-cache", user='root', wait=True,
                       **vm_run_common_args)
                vm.run("gpk-update-viewer;service qubes-update-check start -P",
                       **vm_run_common_args)
        except Exception as ex:
            thread_monitor.set_error_msg(str(ex))
            thread_monitor.set_finished()
            return
        thread_monitor.set_finished()


    @pyqtSlot(name='on_action_run_command_in_vm_triggered')
    def action_run_command_in_vm_triggered(self):
        vm = self.get_selected_vm()

        (command_to_run, ok) = QInputDialog.getText(self, 'Qubes command entry',
                'Run command in <b>'+vm.name+'</b>:')
        if not ok or command_to_run == "":
            return
        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.do_run_command_in_vm, args=(vm, str(command_to_run), thread_monitor))
        thread.daemon = True
        thread.start()

        while not thread_monitor.is_finished():
            app.processEvents()
            time.sleep (0.2)

        if not thread_monitor.success:
            QMessageBox.warning (None, "Error while running command", "Exception while running command:<br>{0}".format(thread_monitor.error_msg))


    def do_run_command_in_vm(self, vm, command_to_run, thread_monitor):
        try:
            vm.run(command_to_run, verbose=False, autostart=True,
                    notify_function=lambda lvl, msg: trayIcon.showMessage(msg, msecs=3000) )
        except Exception as ex:
            thread_monitor.set_error_msg(str(ex))
        thread_monitor.set_finished()

    @pyqtSlot(name='on_action_set_keyboard_layout_triggered')
    def action_set_keyboard_layout_triggered(self):
        vm = self.get_selected_vm()
        vm.run('qubes-change-keyboard-layout', verbose = False)

    @pyqtSlot(name='on_action_showallvms_triggered')
    def action_showallvms_triggered(self):
        self.show_inactive_vms = self.action_showallvms.isChecked()

        self.showhide_vms(self.show_inactive_vms, self.show_internal_vms)
        self.set_table_geom_size()
        if self.settings_loaded:
            self.manager_settings.setValue('view/show_inactive_vms', self.show_inactive_vms)
            self.manager_settings.sync()

    @pyqtSlot(name='on_action_showinternalvms_triggered')
    def action_showinternalvms_triggered(self):
        self.show_internal_vms = self.action_showinternalvms.isChecked()

        self.showhide_vms(self.show_inactive_vms, self.show_internal_vms)
        self.set_table_geom_size()
        if self.settings_loaded:
            self.manager_settings.setValue('view/show_internal_vms', self.show_internal_vms)
            self.manager_settings.sync()

    @pyqtSlot(name='on_action_editfwrules_triggered')
    def action_editfwrules_triggered(self):
        vm = self.get_selected_vm()
        settings_window = VMSettingsWindow(vm, app, self.qvm_collection, "firewall")
        settings_window.exec_()

    @pyqtSlot(name='on_action_global_settings_triggered')
    def action_global_settings_triggered(self):
        global_settings_window = GlobalSettingsWindow(app, self.qvm_collection)
        global_settings_window.exec_()


    @pyqtSlot(name='on_action_restore_triggered')
    def action_restore_triggered(self):
        restore_window = RestoreVMsWindow(app, self.qvm_collection, self.blk_manager)
        restore_window.exec_()

    @pyqtSlot(name='on_action_backup_triggered')
    def action_backup_triggered(self):
        backup_window = BackupVMsWindow(app, self.qvm_collection, self.blk_manager, self.shutdown_vm)
        backup_window.exec_()


    def showhide_menubar(self, checked):
        self.menubar.setVisible(checked)
        self.set_table_geom_size()
        if not checked:
            self.context_menu.addAction(self.action_menubar)
        else:
            self.context_menu.removeAction(self.action_menubar)
        if self.settings_loaded:
            self.manager_settings.setValue('view/menubar_visible', checked)
            self.manager_settings.sync()

    def showhide_toolbar(self, checked):
        self.toolbar.setVisible(checked)
        self.set_table_geom_size()
        if not checked:
            self.context_menu.addAction(self.action_toolbar)
        else:
            self.context_menu.removeAction(self.action_toolbar)
        if self.settings_loaded:
            self.manager_settings.setValue('view/toolbar_visible', checked)
            self.manager_settings.sync()

    def showhide_column(self, col_num, show):
        self.table.setColumnHidden( col_num, not show)
        self.set_table_geom_size()
        val = 1 if show else -1
        self.visible_columns_count += val

        if self.visible_columns_count == 1:
            #disable hiding the last one
            for c in self.columns_actions:
                if self.columns_actions[c].isChecked():
                    self.columns_actions[c].setEnabled(False)
                    break
        elif self.visible_columns_count == 2 and val == 1:
            #enable hiding previously disabled column
            for c in self.columns_actions:
                if not self.columns_actions[c].isEnabled():
                    self.columns_actions[c].setEnabled(True)
                    break

        if self.settings_loaded:
            col_name = [name for name in self.columns_indices.keys() if self.columns_indices[name] == col_num][0]
            self.manager_settings.setValue('columns/%s' % col_name, show)
            self.manager_settings.sync()

    def on_action_vm_type_toggled(self, checked):
        self.showhide_column( self.columns_indices['Type'], checked)

    def on_action_label_toggled(self, checked):
        self.showhide_column( self.columns_indices['Label'], checked)

    def on_action_name_toggled(self, checked):
        self.showhide_column( self.columns_indices['Name'], checked)

    def on_action_state_toggled(self, checked):
        self.showhide_column( self.columns_indices['State'], checked)

    def on_action_internal_toggled(self, checked):
        self.showhide_column( self.columns_indices['Internal'], checked)

    def on_action_ip_toggled(self, checked):
        self.showhide_column( self.columns_indices['IP'], checked)

    def on_action_backups_toggled(self, checked):
        self.showhide_column( self.columns_indices['Backups'], checked)

    def on_action_last_backup_toggled(self, checked):
        self.showhide_column( self.columns_indices['Last backup'], checked)

    def on_action_template_toggled(self, checked):
        self.showhide_column( self.columns_indices['Template'], checked)

    def on_action_netvm_toggled(self, checked):
        self.showhide_column( self.columns_indices['NetVM'], checked)

    def on_action_cpu_toggled(self, checked):
        self.showhide_column( self.columns_indices['CPU'], checked)

    def on_action_cpu_graph_toggled(self, checked):
        self.showhide_column( self.columns_indices['CPU Graph'], checked)

    def on_action_mem_toggled(self, checked):
        self.showhide_column( self.columns_indices['MEM'], checked)

    def on_action_mem_graph_toggled(self, checked):
        self.showhide_column( self.columns_indices['MEM Graph'], checked)

    def on_action_prefmem_toggled(self, checked):
        self.showhide_column( self.columns_indices['Req MEM'], checked)

    def on_action_prefmem_graph_toggled(self, checked):
        self.showhide_column( self.columns_indices['Req MEM Graph'], checked)

    def on_action_size_on_disk_toggled(self, checked):
        self.showhide_column( self.columns_indices['Size'], checked)


    @pyqtSlot(name='on_action_about_qubes_triggered')
    def action_about_qubes_triggered(self):
        release_file = open('/etc/qubes-release', 'r')
        release = release_file.read()
        release_file.close()
        QMessageBox.about(self, "About...", "<b>Qubes OS</b><br><br>%s" % release)


    def createPopupMenu(self):
        menu = QMenu()
        menu.addAction(self.action_toolbar)
        menu.addAction(self.action_menubar)
        return menu

    def open_tools_context_menu(self, widget, point):
        self.tools_context_menu.exec_(widget.mapToGlobal(point))


    @pyqtSlot('const QPoint&')
    def open_context_menu(self, point):
        vm = self.get_selected_vm()

        running = vm.is_running()

        #logs menu
        self.logs_menu.clear()
        if vm.qid == 0:
            text = "/var/log/xen/console/hypervisor.log"
            action = self.logs_menu.addAction(QIcon(":/log.png"), text)
            action.setData(QVariant(text))
            self.logs_menu.setEnabled(True)
        else:
            menu_empty = True
            text = "/var/log/xen/console/guest-"+vm.name+".log"
            if os.path.exists(text):
                action = self.logs_menu.addAction(QIcon(":/log.png"), text)
                action.setData(QVariant(text))
                menu_empty = False

            text = "/var/log/xen/console/guest-"+vm.name+"-dm.log"
            if os.path.exists(text):
                action = self.logs_menu.addAction(QIcon(":/log.png"), text)
                action.setData(QVariant(text))
                menu_empty = False

            text = "/var/log/qubes/guid."+vm.name+".log"
            if os.path.exists(text):
                action = self.logs_menu.addAction(QIcon(":/log.png"), text)
                action.setData(QVariant(text))
                menu_empty = False

            text = "/var/log/qubes/qrexec."+vm.name+".log"
            if os.path.exists(text):
                action = self.logs_menu.addAction(QIcon(":/log.png"), text)
                action.setData(QVariant(text))
                menu_empty = False

            self.logs_menu.setEnabled(not menu_empty)

        # blk menu
        if not running:
            self.blk_menu.setEnabled(False)
        else:
            self.blk_menu.clear()
            self.blk_menu.setEnabled(True)

            self.blk_manager.blk_lock.acquire()
            if len(self.blk_manager.attached_devs) > 0 :
                for d in self.blk_manager.attached_devs:
                    if self.blk_manager.attached_devs[d]['attached_to']['vm'] == vm.name:
                        text = "Detach " + d + " " + unicode(self.blk_manager.attached_devs[d]['size']) + " " + self.blk_manager.attached_devs[d]['desc']
                        action = self.blk_menu.addAction(QIcon(":/remove.png"), text)
                        action.setData(QVariant(d))

            if len(self.blk_manager.free_devs) > 0:
                for d in self.blk_manager.free_devs:
                    if d.startswith(vm.name):
                        continue
                    # skip partitions heuristic
                    if d[-1].isdigit() and d[0:-1] in self.blk_manager.current_blk:
                        continue
                    text = "Attach  " + d + " " + unicode(self.blk_manager.free_devs[d]['size']) + " " + self.blk_manager.free_devs[d]['desc']
                    action = self.blk_menu.addAction(QIcon(":/add.png"), text)
                    action.setData(QVariant(d))

            self.blk_manager.blk_lock.release()

            if self.blk_menu.isEmpty():
                self.blk_menu.setEnabled(False)

        self.context_menu.exec_(self.table.mapToGlobal(point))

    @pyqtSlot('QAction *')
    def show_log(self, action):
        log = str(action.data().toString())
        log_dialog = LogDialog(app, log)
        log_dialog.exec_()


    @pyqtSlot('QAction *')
    def attach_dettach_device_triggered(self, action):
        dev = str(action.data().toString())
        vm = self.get_selected_vm()

        self.blk_manager.blk_lock.acquire()
        try:
            if dev in self.blk_manager.attached_devs:
                self.blk_manager.detach_device(vm, dev)
            else:
                self.blk_manager.attach_device(vm, dev)
            self.blk_manager.blk_lock.release()
        except QubesException as e:
            self.blk_manager.blk_lock.release()
            QMessageBox.critical(None, "Block attach/detach error!", str(e))


class QubesTrayIcon(QSystemTrayIcon):
    def __init__(self, icon, blk_manager):
        QSystemTrayIcon.__init__(self, icon)
        self.menu = QMenu()

        action_showmanager = self.createAction ("Open VM Manager", slot=show_manager, icon="qubes")
        action_backup = self.createAction ("Make backup")
        action_preferences = self.createAction ("Preferences")
        action_set_netvm = self.createAction ("Set default NetVM", icon="networking")
        action_sys_info = self.createAction ("System Info", icon="dom0")
        action_exit = self.createAction ("Exit", slot=exit_app)

        action_backup.setDisabled(True)
        action_preferences.setDisabled(True)
        action_set_netvm.setDisabled(True)
        action_sys_info.setDisabled(True)

        self.blk_manager = blk_manager

        self.blk_menu = QMenu(self.menu)
        self.blk_menu.setTitle("Block devices")
        action_blk_menu = self.createAction("Block devices")
        action_blk_menu.setMenu(self.blk_menu)

        self.addActions (self.menu, (action_showmanager,
                                     action_blk_menu,
                                     action_backup,
                                     action_sys_info,
                                     None,
                                     action_preferences,
                                     action_set_netvm,
                                     None,
                                     action_exit))

        self.setContextMenu(self.menu)

        self.connect (self, SIGNAL("activated (QSystemTrayIcon::ActivationReason)"), self.icon_clicked)

        self.tray_notifier_type = None
        self.tray_notifier = QDBusInterface("org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications", session_bus)
        srv_info = self.tray_notifier.call("GetServerInformation")
        if srv_info.type() == QDBusMessage.ReplyMessage and len(srv_info
                .arguments()) > 1:
            self.tray_notifier_type = srv_info.arguments()[1]

        if os.path.exists(table_widgets.qubes_dom0_updates_stat_file):
            self.showMessage("Qubes dom0 updates available.", msecs=0)


    def update_blk_menu(self):
        global manager_window

        def create_vm_submenu(dev):
            blk_vm_menu = QMenu(self.blk_menu)
            blk_vm_menu.triggered.connect(
                lambda a, d=dev: self.attach_device_triggered(a, dev))
            for vm in sorted(manager_window.qvm_collection.values(),
                             key=lambda x: x.name):
                if not vm.is_running():
                    continue
                if vm.qid == 0:
                    # skip dom0 to prevent (fatal) mistakes
                    continue
                action = blk_vm_menu.addAction(QIcon(":/add.png"), vm.name)
                action.setData(QVariant(vm))
            return blk_vm_menu

        self.blk_menu.clear()
        self.blk_menu.setEnabled(True)

        self.blk_manager.blk_lock.acquire()
        if len(self.blk_manager.attached_devs) > 0 :
            for d in self.blk_manager.attached_devs:
                vm = self.blk_manager.qvm_collection.get_vm_by_name(
                    self.blk_manager.attached_devs[d]['attached_to']['vm']
                )
                text = "Detach %s %s (%s) from %s" % (
                    d,
                    self.blk_manager.attached_devs[d]['desc'],
                    unicode(self.blk_manager.attached_devs[d]['size']),
                    vm.name)
                action = self.blk_menu.addAction(QIcon(":/remove.png"), text)
                action.setData(QVariant(d))
                action.triggered.connect(
                    lambda b, a=action: self.dettach_device_triggered(a))


        if len(self.blk_manager.free_devs) > 0:
            for d in self.blk_manager.free_devs:
                # skip partitions heuristic
                if d[-1].isdigit() and d[0:-1] in self.blk_manager.current_blk:
                    continue
                text = "Attach  %s %s %s" % (
                    d,
                    unicode(self.blk_manager.free_devs[d]['size']),
                    self.blk_manager.free_devs[d]['desc']
                )
                action = self.blk_menu.addAction(QIcon(":/add.png"), text)
                action.setMenu(create_vm_submenu(d))

        self.blk_manager.blk_lock.release()

        if self.blk_menu.isEmpty():
            self.blk_menu.setEnabled(False)

    @pyqtSlot('QAction *')
    def attach_device_triggered(self, action, dev):
        vm = action.data().toPyObject()

        self.blk_manager.blk_lock.acquire()
        try:
            self.blk_manager.attach_device(vm, dev)
            self.blk_manager.blk_lock.release()
        except QubesException as e:
            self.blk_manager.blk_lock.release()
            QMessageBox.critical(None, "Block attach/detach error!", str(e))

    @pyqtSlot('QAction *')
    def dettach_device_triggered(self, action):
        dev = str(action.data().toString())
        vm = self.blk_manager.qvm_collection.get_vm_by_name(
            self.blk_manager.attached_devs[dev]['attached_to']['vm']
        )


        self.blk_manager.blk_lock.acquire()
        try:
            self.blk_manager.detach_device(vm, dev)
            self.blk_manager.blk_lock.release()
        except QubesException as e:
            self.blk_manager.blk_lock.release()
            QMessageBox.critical(None, "Block attach/detach error!", str(e))


    def icon_clicked(self, reason):
        if reason == QSystemTrayIcon.Context:
            self.update_blk_menu()
            # Handle the right click normally, i.e. display the context menu
            return
        else:
            bring_manager_to_front()

    def addActions(self, target, actions):
        for action in actions:
            if action is None:
                target.addSeparator()
            else:
                target.addAction(action)

    def showMessage(self, message, msecs):
        # QtDBus bindings doesn't use introspection to get proper method
        # parameters types, so must cast explicitly
        v_replace_id = QVariant(0)
        v_replace_id.convert(QVariant.UInt)
        v_actions = QVariant([])
        v_actions.convert(QVariant.StringList)
        if self.tray_notifier_type == "KDE":
            message = message.replace('\n', '<br/>\n')
        self.tray_notifier.call("Notify", "Qubes", v_replace_id,
                "qubes-manager", "Qubes VM Manager",
                message, v_actions, QVariant.fromMap({}), msecs)

    def createAction(self, text, slot=None, shortcut=None, icon=None,
                     tip=None, checkable=False, signal="triggered()"):
        action = QAction(text, self)
        if icon is not None:
            action.setIcon(QIcon(":/%s.png" % icon))
        if shortcut is not None:
            action.setShortcut(shortcut)
        if tip is not None:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        if slot is not None:
            self.connect(action, SIGNAL(signal), slot)
        if checkable:
            action.setCheckable(True)
        return action

class QubesDbusNotifyServerAdaptor(QDBusAbstractAdaptor):
    """ This provides the DBus adaptor to the outside world"""

    Q_CLASSINFO("D-Bus Interface", dbus_interface)

    @pyqtSlot(str, str)
    def notify_error(self, vmname, message):
        vm = self.parent().qvm_collection.get_vm_by_name(vmname)
        if vm:
            self.parent().set_error(vm.qid, message)
        else:
            # ignore VM-not-found error
            pass

    @pyqtSlot(str, str)
    def clear_error_exact(self, vmname, message):
        vm = self.parent().qvm_collection.get_vm_by_name(vmname)
        if vm:
            self.parent().clear_error_exact(vm.qid, message)
        else:
            # ignore VM-not-found error
            pass

    @pyqtSlot(str)
    def clear_error(self, vmname):
        vm = self.parent().qvm_collection.get_vm_by_name(vmname)
        if vm:
            self.parent().clear_error(vm.qid)
        else:
            # ignore VM-not-found error
            pass

    @pyqtSlot()
    def show_manager(self):
        bring_manager_to_front()

def get_frame_size():
    w = 0
    h = 0
    cmd = ['/usr/bin/xprop', '-name', 'Qubes VM Manager', '|', 'grep', '_NET_FRAME_EXTENTS']
    xprop = subprocess.Popen(cmd, stdout = subprocess.PIPE)
    for l in xprop.stdout:
        line = l.split('=')
        if len(line) == 2:
            line = line[1].strip().split(',')
            if len(line) == 4:
                w = int(line[0].strip())+ int(line[1].strip())
                h = int(line[2].strip())+ int(line[3].strip())
                break
    #in case of some weird window managers we have to assume sth...
    if w<= 0:
        w = 10
    if h <= 0:
        h = 30

    manager_window.frame_width = w
    manager_window.frame_height = h
    return

def show_manager():
    manager_window.show()
    manager_window.set_table_geom_size()
    manager_window.repaint()
    manager_window.update_table(out_of_schedule=True)
    app.processEvents()

    get_frame_size()
    #print manager_window.frame_width, " x ", manager_window.frame_height
    manager_window.set_table_geom_size()

def bring_manager_to_front():

    if manager_window.isVisible():
        subprocess.check_call(['/usr/bin/wmctrl', '-R', str(manager_window.windowTitle())])

    else:
        show_manager()

def show_running_manager_via_dbus():
    global system_bus
    if system_bus is None:
        system_bus = QDBusConnection.systemBus()

    qubes_manager = QDBusInterface('org.qubesos.QubesManager',
            '/org/qubesos/QubesManager', 'org.qubesos.QubesManager', system_bus)
    qubes_manager.call('show_manager')

def exit_app():
    notifier.stop()
    app.exit()

# Bases on the original code by:
# Copyright (c) 2002-2007 Pascal Varet <p.varet@gmail.com>

def handle_exception( exc_type, exc_value, exc_traceback ):
    import sys
    import os.path
    import traceback

    filename, line, dummy, dummy = traceback.extract_tb( exc_traceback ).pop()
    filename = os.path.basename( filename )
    error    = "%s: %s" % ( exc_type.__name__, exc_value )

    strace = ""
    stacktrace = traceback.extract_tb( exc_traceback )
    while len(stacktrace) > 0:
        (filename, line, func, txt) = stacktrace.pop()
        strace += "----\n"
        strace += "line: %s\n" %txt
        strace += "func: %s\n" %func
        strace += "line no.: %d\n" %line
        strace += "file: %s\n" %filename

    msg_box = QMessageBox()
    msg_box.setDetailedText(strace)
    msg_box.setIcon(QMessageBox.Critical)
    msg_box.setWindowTitle( "Houston, we have a problem...")
    msg_box.setText("Whoops. A critical error has occured. This is most likely a bug "
                    "in Qubes Manager.<br><br>"
                    "<b><i>%s</i></b>" % error +
                    "<br/>at line <b>%d</b><br/>of file %s.<br/><br/>"
                    % ( line, filename ))

    msg_box.exec_()

def sighup_handler(signum, frame):
    os.execl("/usr/bin/qubes-manager", "qubes-manager")

try:
    import objgraph
    def sighandler_show_growth(sig, frame):
        print('== objgraph.show_growth():')
        objgraph.show_growth(shortnames=False)

except ImportError:
    print("WARNING: objgraph module not installed")
    def sighandler_show_growth(sig, frame):
        print("ERROR: objgraph module not installed")


def sighandler_pdb(sig, frame):
    import pdb
    pyqtRemoveInputHook()
    pdb.set_trace()

def main():
    # Avoid starting more than one instance of the app
    lock = QubesDaemonPidfile ("qubes-manager")
    if lock.pidfile_exists():
        if lock.read_pid() == os.getpid():
            pass
        elif lock.pidfile_is_stale():
            lock.remove_pidfile()
            print "Removed stale pidfile (has the previous daemon instance crashed?)."
        else:
            show_running_manager_via_dbus()
            exit (0)

    lock.create_pidfile()

    signal.signal(signal.SIGHUP, sighup_handler)
    signal.signal(signal.SIGINT, sighandler_show_growth)
    signal.signal(signal.SIGQUIT, sighandler_pdb)

    global qubes_host
    qubes_host = QubesHost()

    global app
    app = QApplication(sys.argv)
    app.setOrganizationName("The Qubes Project")
    app.setOrganizationDomain("http://qubes-os.org")
    app.setApplicationName("Qubes VM Manager")
    app.setWindowIcon(QIcon.fromTheme("qubes-manager"))
    app.setAttribute(Qt.AA_DontShowIconsInMenus, False)

    sys.excepthook = handle_exception

    global session_bus
    session_bus = QDBusConnection.sessionBus()

    qvm_collection = QubesVmCollection()
    qvm_collection.lock_db_for_reading()
    qvm_collection.load()
    qvm_collection.unlock_db()

    blk_manager = QubesBlockDevicesManager(qvm_collection)

    global trayIcon
    trayIcon = QubesTrayIcon(QIcon.fromTheme("qubes-manager"), blk_manager)

    global manager_window
    manager_window = VmManagerWindow(qvm_collection, blk_manager)

    global wm
    wm = WatchManager()
    global notifier
    notifier = ThreadedNotifier(wm, QubesManagerFileWatcher(manager_window.mark_table_for_update))
    notifier.start()
    wm.add_watch(system_path["qubes_store_filename"], EventsCodes.OP_FLAGS.get('IN_MODIFY'))
    wm.add_watch(os.path.dirname(system_path["qubes_store_filename"]), EventsCodes.OP_FLAGS.get('IN_MOVED_TO'))
    wm.add_watch(qubes_clipboard_info_file, EventsCodes.OP_FLAGS.get('IN_CLOSE_WRITE'))
    wm.add_watch(os.path.dirname(qubes_clipboard_info_file), EventsCodes.OP_FLAGS.get('IN_CREATE'))
    wm.add_watch(os.path.dirname(table_widgets.qubes_dom0_updates_stat_file),
                 EventsCodes.OP_FLAGS.get('IN_CREATE'))

    global system_bus
    system_bus = QDBusConnection.systemBus()
    system_bus.registerService('org.qubesos.QubesManager')
    system_bus.registerObject(dbus_object_path, manager_window)

    trayIcon.show()

    show_manager()
    app.exec_()

    lock.remove_pidfile()
    trayIcon = None

if __name__ == "__main__":
    main()
