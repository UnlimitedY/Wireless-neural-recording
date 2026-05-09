# -*- coding: utf-8 -*-
import serial
import serial.tools.list_ports
import binascii
import string
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, 
    QPushButton, QComboBox, QTextEdit, QMessageBox
)
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QFont


class RFPowerControlWidget(QWidget):
    """RF功率控制GUI组件"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.serial_connection = None
        self.is_connected = False
        self.setup_ui()
        self.refresh_serial_ports()
        
    def setup_ui(self):
        """设置用户界面"""
        layout = QVBoxLayout(self)
        
        # 串口连接组
        connection_group = self.create_connection_group()
        layout.addWidget(connection_group)
        
        # RF控制组
        control_group = self.create_control_group()
        layout.addWidget(control_group)
        
        # 状态显示组
        status_group = self.create_status_group()
        layout.addWidget(status_group)
        
        # 设置连接
        self.setup_connections()
        
    def create_connection_group(self):
        """创建串口连接组"""
        group = QGroupBox("Serial Connection")
        layout = QVBoxLayout(group)
        
        # 串口选择行
        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("Port:"))
        
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(120)
        port_layout.addWidget(self.port_combo)
        
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setMaximumWidth(60)
        port_layout.addWidget(self.refresh_btn)
        
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setMaximumWidth(60)
        self.connect_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 5px;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        port_layout.addWidget(self.connect_btn)
        
        layout.addLayout(port_layout)
        
        # 连接状态
        self.connection_status = QLabel("Status: Disconnected")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        layout.addWidget(self.connection_status)
        
        return group
        
    def create_control_group(self):
        """创建RF控制组"""
        group = QGroupBox("RF Power Control")
        layout = QVBoxLayout(group)
        
        # 控制按钮行
        button_layout = QHBoxLayout()
        
        self.power_on_btn = QPushButton("RF Power On")
        self.power_on_btn.setEnabled(False)
        self.power_on_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover:enabled {
                background-color: #1976D2;
            }
            QPushButton:pressed:enabled {
                background-color: #1565C0;
            }
            QPushButton:disabled {
                background-color: #CCCCCC;
                color: #666666;
            }
        """)
        button_layout.addWidget(self.power_on_btn)
        
        self.power_off_btn = QPushButton("RF Power Off")
        self.power_off_btn.setEnabled(False)
        self.power_off_btn.setStyleSheet("""
            QPushButton {
                background-color: #F44336;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover:enabled {
                background-color: #D32F2F;
            }
            QPushButton:pressed:enabled {
                background-color: #C62828;
            }
            QPushButton:disabled {
                background-color: #CCCCCC;
                color: #666666;
            }
        """)
        button_layout.addWidget(self.power_off_btn)
        
        self.status_btn = QPushButton("Query Status")
        self.status_btn.setEnabled(False)
        self.status_btn.setStyleSheet("""
            QPushButton {
                background-color: #FF9800;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover:enabled {
                background-color: #F57C00;
            }
            QPushButton:pressed:enabled {
                background-color: #EF6C00;
            }
            QPushButton:disabled {
                background-color: #CCCCCC;
                color: #666666;
            }
        """)
        button_layout.addWidget(self.status_btn)
        
        layout.addLayout(button_layout)
        
        # RF状态显示
        self.rf_status = QLabel("RF Status: Unknown")
        self.rf_status.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self.rf_status)
        
        return group
        
    def create_status_group(self):
        """创建状态显示组"""
        group = QGroupBox("Activity Log")
        layout = QVBoxLayout(group)
        
        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(150)
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #f5f5f5;
                border: 1px solid #ddd;
                border-radius: 4px;
                padding: 5px;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 10px;
            }
        """)
        layout.addWidget(self.log_text)
        
        # 清除日志按钮
        clear_btn = QPushButton("Clear Log")
        clear_btn.setMaximumWidth(80)
        clear_btn.clicked.connect(self.log_text.clear)
        layout.addWidget(clear_btn)
        
        return group
        
    def setup_connections(self):
        """设置信号连接"""
        self.refresh_btn.clicked.connect(self.refresh_serial_ports)
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.power_on_btn.clicked.connect(self.power_on)
        self.power_off_btn.clicked.connect(self.power_off)
        self.status_btn.clicked.connect(self.query_status)
        
    def refresh_serial_ports(self):
        """刷新串口列表"""
        try:
            self.port_combo.clear()
            ports = serial.tools.list_ports.comports()
            
            if ports:
                for port in ports:
                    self.port_combo.addItem(f"{port.device} - {port.description}")
                self.log_message(f"Found {len(ports)} serial ports")
            else:
                self.port_combo.addItem("No ports found")
                self.log_message("No serial ports available")
                
        except Exception as e:
            self.log_message(f"Failed to refresh ports: {str(e)}")
            
    def toggle_connection(self):
        """切换串口连接状态"""
        if not self.is_connected:
            self.connect_serial()
        else:
            self.disconnect_serial()
            
    def connect_serial(self):
        """连接串口"""
        try:
            if self.port_combo.currentText() == "No ports found":
                QMessageBox.warning(self, "Warning", "Please select a valid port first")
                return
                
            port_text = self.port_combo.currentText()
            port_name = port_text.split(" - ")[0]
            
            self.serial_connection = serial.Serial(
                port=port_name,
                baudrate=9600,
                timeout=0.5
            )
            
            self.is_connected = True
            self.connect_btn.setText("Disconnect")
            self.connect_btn.setStyleSheet("""
                QPushButton {
                    background-color: #F44336;
                    color: white;
                    border: none;
                    padding: 5px;
                    border-radius: 3px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #D32F2F;
                }
                QPushButton:pressed {
                    background-color: #C62828;
                }
            """)
            
            self.connection_status.setText(f"Status: Connected to {port_name}")
            self.connection_status.setStyleSheet("color: green; font-weight: bold;")
            
            # 启用控制按钮
            self.power_on_btn.setEnabled(True)
            self.power_off_btn.setEnabled(True)
            self.status_btn.setEnabled(True)
            
            self.log_message(f"Connected to serial port: {port_name}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to connect serial port: {str(e)}")
            self.log_message(f"Failed to connect serial port: {str(e)}")
            
    def disconnect_serial(self):
        """断开串口连接"""
        try:
            if self.serial_connection and self.serial_connection.is_open:
                self.serial_connection.close()
                
            self.is_connected = False
            self.serial_connection = None
            
            self.connect_btn.setText("Connect")
            self.connect_btn.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    padding: 5px;
                    border-radius: 3px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:pressed {
                    background-color: #3d8b40;
                }
            """)
            
            self.connection_status.setText("Status: Disconnected")
            self.connection_status.setStyleSheet("color: red; font-weight: bold;")
            
            # 禁用控制按钮
            self.power_on_btn.setEnabled(False)
            self.power_off_btn.setEnabled(False)
            self.status_btn.setEnabled(False)
            
            self.rf_status.setText("RF Status: Unknown")
            self.log_message("Serial connection closed")
            
        except Exception as e:
            self.log_message(f"Failed to disconnect: {str(e)}")
            
    def power_on(self):
        """打开RF功率"""
        try:
            if not self.is_connected or not self.serial_connection:
                QMessageBox.warning(self, "Warning", "Please connect the serial port first")
                return
                
            # 发送打开命令: A0 01 03 A4
            command = bytes.fromhex('A0 01 03 A4')
            self.serial_connection.write(command)
            self.log_message("Sent RF power on command: A0 01 03 A4")
            
            time.sleep(0.1)
            
            # 读取响应
            n = self.serial_connection.in_waiting
            if n > 0:
                response = self.serial_connection.read(n)
                response_hex = binascii.b2a_hex(response).decode()
                self.log_message(f"Received response: {response_hex}")
                
                # 解析状态
                if len(response_hex) >= 2:
                    status = response_hex[-2]
                    if status == '2':
                        self.rf_status.setText("RF Status: On")
                        self.rf_status.setStyleSheet("color: green; font-weight: bold; font-size: 12px;")
                    else:
                        self.rf_status.setText("RF Status: Off")
                        self.rf_status.setStyleSheet("color: red; font-weight: bold; font-size: 12px;")
            else:
                self.log_message("No response received")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to turn RF power on: {str(e)}")
            self.log_message(f"Failed to turn RF power on: {str(e)}")
            
    def power_off(self):
        """关闭RF功率"""
        try:
            if not self.is_connected or not self.serial_connection:
                QMessageBox.warning(self, "Warning", "Please connect the serial port first")
                return
                
            # 发送关闭命令: A0 01 02 A3
            command = bytes.fromhex('A0 01 02 A3')
            self.serial_connection.write(command)
            self.log_message("Sent RF power off command: A0 01 02 A3")
            
            time.sleep(0.1)
            
            # 读取响应
            n = self.serial_connection.in_waiting
            if n > 0:
                response = self.serial_connection.read(n)
                response_hex = binascii.b2a_hex(response).decode()
                self.log_message(f"Received response: {response_hex}")
                
                # 解析状态
                if len(response_hex) >= 2:
                    status = response_hex[-2]
                    if status == '1':
                        self.rf_status.setText("RF Status: Off")
                        self.rf_status.setStyleSheet("color: red; font-weight: bold; font-size: 12px;")
                    else:
                        self.rf_status.setText("RF Status: On")
                        self.rf_status.setStyleSheet("color: green; font-weight: bold; font-size: 12px;")
            else:
                self.log_message("No response received")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to turn RF power off: {str(e)}")
            self.log_message(f"Failed to turn RF power off: {str(e)}")
            
    def query_status(self):
        """查询RF状态"""
        try:
            if not self.is_connected or not self.serial_connection:
                QMessageBox.warning(self, "Warning", "Please connect the serial port first")
                return
                
            # 发送查询命令: A0 01 05 A6
            command = bytes.fromhex('A0 01 05 A6')
            self.serial_connection.write(command)
            self.log_message("Sent query status command: A0 01 05 A6")
            
            time.sleep(0.1)
            
            # 读取响应
            n = self.serial_connection.in_waiting
            if n > 0:
                response = self.serial_connection.read(n)
                response_hex = binascii.b2a_hex(response).decode()
                self.log_message(f"Received response: {response_hex}")
                
                # 解析状态
                if len(response_hex) >= 2:
                    status = response_hex[-2]
                    if status == '2':
                        self.rf_status.setText("RF Status: On")
                        self.rf_status.setStyleSheet("color: green; font-weight: bold; font-size: 12px;")
                    elif status == '1':
                        self.rf_status.setText("RF Status: Off")
                        self.rf_status.setStyleSheet("color: red; font-weight: bold; font-size: 12px;")
                    else:
                        self.rf_status.setText("RF Status: Unknown")
                        self.rf_status.setStyleSheet("color: orange; font-weight: bold; font-size: 12px;")
            else:
                self.log_message("No response received")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Query failed: {str(e)}")
            self.log_message(f"Query failed: {str(e)}")
            
    def log_message(self, message):
        """添加日志消息"""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")
        
    def closeEvent(self, event):
        """关闭事件处理"""
        if self.is_connected:
            self.disconnect_serial()
        event.accept()


# 保持原有的serialPowerControl类以兼容现有代码
class serialPowerControl():
    def __init__(self):
        self.serialPort = "COM31"  # 串口
        self.baudRate = 9600  # 波特率
        self.s = serial.Serial(self.serialPort, self.baudRate, timeout=0.5)
        self.status = 0 
    def open(self):
        d=bytes.fromhex('A0 01 03 A4')
        self.s.write(d)
        print("Relay on")
        time.sleep(0.1)
        n = self.s.in_waiting
        self.status = str(binascii.b2a_hex(self.s.read(n)))[2:-1]
    def close(self):
        d = bytes.fromhex('A0 01 02 A3')
        self.s.write(d)
        print("Relay off")
        time.sleep(0.1)
        n = self.s.in_waiting
        self.status = str(binascii.b2a_hex(self.s.read(n)))[-2] # open: 2; close: 1

    def read_status(self):
        """读取继电器状态"""
        if not self.serial_connection:
            self.log_message("Error: serial not connected")
            return
        
        try:
            d = bytes.fromhex('A0 01 05 A6')
            self.serial_connection.write(d)
            time.sleep(0.1)
            n = self.serial_connection.in_waiting
            status = str(binascii.b2a_hex(self.serial_connection.read(n)))[-2]
            status_text = "On" if status == '2' else "Off"
            self.log_message(f"Relay status: {status_text}")
        except Exception as e:
            self.log_message(f"Failed to read status: {str(e)}")


# 测试代码
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    
    # 创建RF功率控制窗口
    rf_control = RFPowerControlWidget()
    rf_control.setWindowTitle("RF Power Control")
    rf_control.resize(400, 500)
    rf_control.show()
    
    sys.exit(app.exec())


#收发数据
#n = s.inWaiting()
#if n:
#  data = str(binascii.b2a_hex(s.read(n)))[2:-1]
#  print(data)
#通讯协议
#指令通过16进制形式发送
#数据（1）---启始标识（默认为0xA0）
#数据（2）---开关地址码（默认为0x01，标识第1路；0x02标识第2路…依次类推）
#数据（3）---操作数据（0x00为关不反馈，0x01为开不反馈，0x02为关并反馈，0x03为开并反馈，0x04为取反并反馈，0x05为查询状态0X06为闪断并反馈）
#数据（4）---校验码（前面三个数据加和）

#示例
#打开第1路USB开关不反馈：A0 01 01 A2，继电器会吸合，但不会反馈数据
#关闭第1路USB开关不反馈：A0 01 00 A1，继电器会释放，但不会反馈数据

#打开第1路USB开关并反馈：A0 01 03 A4，继电器会吸合，并反馈状态A0 01 01 A2
#关闭第1路USB开关并反馈：A0 01 02 A3，继电器会释放，并反馈状态A0 01 00 A1

#取反第1路USB开关并反馈：A0 01 04 A5，继电器的状态会变化，并反馈最终状态
#关闭第1路USB开关不反馈：A0 01 05 A6，继电器会反馈实时状态
#打开第2路USB开关不反馈：A0 02 01 A3，继电器会吸合，但不会反馈数据
#关闭第2路USB开关不反馈：A0 02 00 A2，继电器会释放，但不会反馈数据
#打开第3路USB开关不反馈：A0 03 01 A4，继电器会吸合，但不会反馈数据
#关闭第3路USB开关不反馈：A0 03 00 A3，继电器会释放，但不会反馈数据
#打开第4路USB开关不反馈：A0 04 01 A5，继电器会吸合，但不会反馈数据
#关闭第4路USB开关不反馈：A0 04 00 A4，继电器会释放，但不会反馈数据
