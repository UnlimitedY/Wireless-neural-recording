/*
 * ESB version LFP central 接收多个下位机上传来的数据并进行数据的预处理通过串口发送到PC
 * 同时接收PC和HABITS通过串口下发的命令，并通过ESB发送给不同的下位机,主要的作用就是实时的转发下位机与上位机之间的数据流动
 * 使用arduino 来作为盾板， spi传输到teensy上，再由teensy上传到PC
 */
#include <zephyr/drivers/clock_control.h>
#include <zephyr/drivers/clock_control/nrf_clock_control.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/irq.h>
#include <zephyr/logging/log.h>
#include <nrf.h>
#include <esb.h>
#include <zephyr/types.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <nrfx_usbd.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/usb/usbd.h>

typedef unsigned char u8_t;
typedef unsigned short u16_t;
typedef unsigned int u32_t;

#define LOG_MODULE_NAME LFP_Recording_central
LOG_MODULE_REGISTER(LOG_MODULE_NAME, LOG_LEVEL_INF); 

#define LED0_NODE DT_ALIAS(led0) 
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(LED0_NODE, gpios);
void LED_hinting(uint32_t interval, uint32_t eventNum){
        // interval: 2000; 1000; 500; 200 
        u16_t events = 0;
        while(1)
        { 
                gpio_pin_toggle_dt(&led);
                k_sleep(K_MSEC(interval));
                events++;
                if(events >= 2*eventNum){
                        break;
                }
        }
}

#define USBTX_PACKETS_SIZE 10 // 10 packets per uart send：no matter the kind of packets
#define COMMAND_FRAME_MAGIC_0 0xA5
#define COMMAND_FRAME_MAGIC_1 0x5A
#define COMMAND_MAX_PAYLOAD_LEN 16
#define COMMAND_RX_BUFFER_SIZE 64
#define COMMAND_QUEUE_SIZE 8
#define COMMAND_TX_RETRY_COUNT 3
#define RELAY_LOCAL_CMD_PREFIX_0 0x52
#define RELAY_LOCAL_CMD_PREFIX_1 0x4C
#define RELAY_LOCAL_CMD_PREFIX_2 0x59
#define RELAY_LOCAL_CMD_REBOOT 0x01
#define RELAY_LOCAL_CMD_SET_ESB_CH_84 0x10
#define RELAY_LOCAL_CMD_SET_ESB_CH_78 0x11
#define RELAY_LOCAL_CMD_SET_ESB_CH_67 0x12
#define RELAY_LOCAL_CMD_SET_ESB_CH_50 0x13
#define RELAY_LOCAL_CMD_SET_ESB_CH_33 0x14
#define RELAY_LOCAL_CMD_SET_ESB_CH_17 0x15
#define RELAY_LOCAL_CMD_SET_ESB_CH_2  0x16
#define RELAY_DEFAULT_ESB_CHANNEL 84

#define USB_TX_RING_BUF_SIZE 65536U
#define USB_TX_DRAIN_CHUNK_SIZE 256U

static const u8_t usb_group_end_marker[4] = {0x25, 0x26, 0x27, 0x28};
static u8_t usb_tx_ring_buffer[USB_TX_RING_BUF_SIZE];
static u8_t usb_tx_drain_buffer[USB_TX_DRAIN_CHUNK_SIZE];
static size_t usb_tx_ring_head = 0;
static size_t usb_tx_ring_tail = 0;
static size_t usb_tx_ring_count = 0;
static volatile uint32_t usb_tx_ring_overflow_count = 0;
static volatile uint32_t usb_tx_partial_write_count = 0;
static volatile uint32_t usb_tx_bytes_enqueued = 0;
static volatile uint32_t usb_tx_bytes_sent = 0;
static volatile uint32_t esb_rx_packet_count = 0;

const struct device *dev;

/*****************for alignment*****************/
// timestamp
uint32_t timestamp_HABITS = 0;

/**************************************** esb setup********************************************************/
u16_t drop_packets_num = 0;

u16_t esb_packets_length = 0;
int packets_accumul_num = 0;

u8_t timestamp_alignment_buffer[1000];
u8_t usb_packets_address[8] = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26 ,0x27, 0x28};

// for test
int last_packet_counter[2] = {0, 0};

static struct esb_payload rx_payload; // central rx buffer // 注意这里的esb 也是u16_t
static struct esb_payload tx_payload; // command buffer

u16_t rx_temp_payload[130];

struct relay_command_entry {
	u8_t length;
	u8_t retries_left;
	u8_t data[COMMAND_MAX_PAYLOAD_LEN];
};

static struct relay_command_entry command_queue[COMMAND_QUEUE_SIZE];
static volatile u8_t command_queue_head = 0;
static volatile u8_t command_queue_tail = 0;
static volatile u8_t command_queue_count = 0;
static u8_t command_rx_buffer[COMMAND_RX_BUFFER_SIZE];
static size_t command_rx_buffer_len = 0;
static struct relay_command_entry pending_command;
static volatile bool pending_command_valid = false;
static volatile bool pending_command_inflight = false;
static volatile bool relay_reboot_requested = false;

static int send_len = 0; // recording the number of data usb sent
static int counter_loop = 0; // recording the number of packages

int rf_channel = RELAY_DEFAULT_ESB_CHANNEL;
bool channel_switch = false;
k_tid_t mainThread;

int retssss;

static u8_t relay_command_checksum(u8_t length, const u8_t *data)
{
	u8_t checksum = length ^ COMMAND_FRAME_MAGIC_0 ^ COMMAND_FRAME_MAGIC_1;
	for (u8_t i = 0; i < length; i++)
	{
		checksum ^= data[i];
	}
	return checksum;
}

static int relay_local_command_to_channel(u8_t command_code)
{
	switch (command_code)
	{
	case RELAY_LOCAL_CMD_SET_ESB_CH_84:
		return 84;
	case RELAY_LOCAL_CMD_SET_ESB_CH_78:
		return 78;
	case RELAY_LOCAL_CMD_SET_ESB_CH_67:
		return 67;
	case RELAY_LOCAL_CMD_SET_ESB_CH_50:
		return 50;
	case RELAY_LOCAL_CMD_SET_ESB_CH_33:
		return 33;
	case RELAY_LOCAL_CMD_SET_ESB_CH_17:
		return 17;
	case RELAY_LOCAL_CMD_SET_ESB_CH_2:
		return 2;
	default:
		return -1;
	}
}

static void command_rx_buffer_consume(size_t count)
{
	if (count >= command_rx_buffer_len)
	{
		command_rx_buffer_len = 0;
		return;
	}
	memmove(command_rx_buffer, command_rx_buffer + count, command_rx_buffer_len - count);
	command_rx_buffer_len -= count;
}

static bool command_queue_push(const u8_t *data, u8_t length, u8_t retries_left)
{
	bool pushed = false;
	unsigned int key = irq_lock();
	if (command_queue_count < COMMAND_QUEUE_SIZE)
	{
		struct relay_command_entry *entry = &command_queue[command_queue_tail];
		entry->length = length;
		entry->retries_left = retries_left;
		memcpy(entry->data, data, length);
		command_queue_tail = (command_queue_tail + 1) % COMMAND_QUEUE_SIZE;
		command_queue_count++;
		pushed = true;
	}
	irq_unlock(key);
	return pushed;
}

static bool command_queue_pop_locked(struct relay_command_entry *entry)
{
	if (command_queue_count == 0)
	{
		return false;
	}
	*entry = command_queue[command_queue_head];
	command_queue_head = (command_queue_head + 1) % COMMAND_QUEUE_SIZE;
	command_queue_count--;
	return true;
}

static void try_submit_pending_command(void)
{
	int err;
	struct relay_command_entry command_to_send;
	unsigned int key = irq_lock();
	if (pending_command_inflight)
	{
		irq_unlock(key);
		return;
	}
	if (!pending_command_valid)
	{
		if (!command_queue_pop_locked(&pending_command))
		{
			irq_unlock(key);
			return;
		}
		pending_command_valid = true;
	}
	command_to_send = pending_command;
	pending_command_inflight = true;
	irq_unlock(key);

	tx_payload.length = command_to_send.length;
	memcpy(tx_payload.data, command_to_send.data, command_to_send.length);
	err = esb_write_payload(&tx_payload);
	if (!err)
	{
		return;
	}
	key = irq_lock();
	pending_command_inflight = false;
	irq_unlock(key);
}

static void queue_pc_command_payload(const u8_t *payload, u8_t payload_len)
{
	if (payload_len == 0 || payload_len > COMMAND_MAX_PAYLOAD_LEN)
	{
		return;
	}
	if (!command_queue_push(payload, payload_len, COMMAND_TX_RETRY_COUNT))
	{
		LOG_INF("Command queue full, dropping host command");
		return;
	}
	try_submit_pending_command();
}

static bool handle_local_relay_command(const u8_t *payload, u8_t payload_len)
{
	int selected_channel;
	if (payload_len != 4)
	{
		return false;
	}
	if (payload[0] != RELAY_LOCAL_CMD_PREFIX_0 ||
	    payload[1] != RELAY_LOCAL_CMD_PREFIX_1 ||
	    payload[2] != RELAY_LOCAL_CMD_PREFIX_2)
	{
		return false;
	}
	if (payload[3] == RELAY_LOCAL_CMD_REBOOT)
	{
		relay_reboot_requested = true;
		k_wakeup(mainThread);
		return true;
	}
	selected_channel = relay_local_command_to_channel(payload[3]);
	if (selected_channel >= 0)
	{
		rf_channel = selected_channel;
		channel_switch = true;
		k_wakeup(mainThread);
	}
	return true;
}

static void process_pc_command_bytes(const u8_t *data, size_t length)
{
	if (length == 0)
	{
		return;
	}
	if (length >= COMMAND_RX_BUFFER_SIZE)
	{
		command_rx_buffer_len = 0;
		data += length - (COMMAND_RX_BUFFER_SIZE - 1);
		length = COMMAND_RX_BUFFER_SIZE - 1;
	}
	if (command_rx_buffer_len + length > COMMAND_RX_BUFFER_SIZE)
	{
		command_rx_buffer_consume(command_rx_buffer_len + length - COMMAND_RX_BUFFER_SIZE);
	}
	memcpy(command_rx_buffer + command_rx_buffer_len, data, length);
	command_rx_buffer_len += length;

	while (command_rx_buffer_len >= 4)
	{
		size_t sync_index = 0;
		while ((sync_index + 1) < command_rx_buffer_len)
		{
			if (command_rx_buffer[sync_index] == COMMAND_FRAME_MAGIC_0 &&
			    command_rx_buffer[sync_index + 1] == COMMAND_FRAME_MAGIC_1)
			{
				break;
			}
			sync_index++;
		}
		if (sync_index > 0)
		{
			command_rx_buffer_consume(sync_index);
			if (command_rx_buffer_len < 4)
			{
				return;
			}
		}
		if (command_rx_buffer_len < 4)
		{
			return;
		}

		u8_t payload_len = command_rx_buffer[2];
		size_t frame_len = (size_t)payload_len + 4U;
		if (payload_len == 0 || payload_len > COMMAND_MAX_PAYLOAD_LEN)
		{
			command_rx_buffer_consume(1);
			continue;
		}
		if (command_rx_buffer_len < frame_len)
		{
			return;
		}
		if (command_rx_buffer[3 + payload_len] != relay_command_checksum(payload_len, &command_rx_buffer[3]))
		{
			command_rx_buffer_consume(1);
			continue;
		}
		if (handle_local_relay_command(&command_rx_buffer[3], payload_len))
		{
			command_rx_buffer_consume(frame_len);
			continue;
		}
		queue_pc_command_payload(&command_rx_buffer[3], payload_len);
		command_rx_buffer_consume(frame_len);
	}
}
/**************************************** usb cdc function********************************************************/

static size_t min_size_t(size_t a, size_t b)
{
	return (a < b) ? a : b;
}

static void usb_tx_ring_copy_in_locked(const u8_t *data, size_t length)
{
	if (length == 0)
	{
		return;
	}
	size_t first = min_size_t(length, USB_TX_RING_BUF_SIZE - usb_tx_ring_head);
	memcpy(&usb_tx_ring_buffer[usb_tx_ring_head], data, first);
	if (length > first)
	{
		memcpy(&usb_tx_ring_buffer[0], data + first, length - first);
	}
	usb_tx_ring_head = (usb_tx_ring_head + length) % USB_TX_RING_BUF_SIZE;
	usb_tx_ring_count += length;
	usb_tx_bytes_enqueued += (uint32_t)length;
}

static void usb_tx_kick(void)
{
	if (dev != NULL && device_is_ready(dev))
	{
		uart_irq_tx_enable(dev);
	}
}

static bool usb_tx_enqueue_segments(const u8_t *first_data, size_t first_len,
				    const u8_t *second_data, size_t second_len)
{
	size_t frame_len = first_len + second_len;
	bool accepted = false;

	if (frame_len == 0)
	{
		return true;
	}
	if (frame_len > USB_TX_RING_BUF_SIZE)
	{
		drop_packets_num++;
		usb_tx_ring_overflow_count++;
		return false;
	}

	unsigned int key = irq_lock();
	if ((USB_TX_RING_BUF_SIZE - usb_tx_ring_count) >= frame_len)
	{
		usb_tx_ring_copy_in_locked(first_data, first_len);
		usb_tx_ring_copy_in_locked(second_data, second_len);
		accepted = true;
	}
	else
	{
		drop_packets_num++;
		usb_tx_ring_overflow_count++;
	}
	irq_unlock(key);

	if (accepted)
	{
		usb_tx_kick();
	}
	return accepted;
}

static bool usb_tx_enqueue_bytes(const u8_t *data, size_t length)
{
	return usb_tx_enqueue_segments(data, length, NULL, 0);
}

static void usb_tx_drain_irq(const struct device *uart_dev)
{
	while (uart_irq_tx_ready(uart_dev))
	{
		size_t chunk_len;
		unsigned int key = irq_lock();
		if (usb_tx_ring_count == 0)
		{
			irq_unlock(key);
			uart_irq_tx_disable(uart_dev);
			return;
		}
		chunk_len = min_size_t(usb_tx_ring_count, USB_TX_DRAIN_CHUNK_SIZE);
		chunk_len = min_size_t(chunk_len, USB_TX_RING_BUF_SIZE - usb_tx_ring_tail);
		memcpy(usb_tx_drain_buffer, &usb_tx_ring_buffer[usb_tx_ring_tail], chunk_len);
		irq_unlock(key);

		send_len = uart_fifo_fill(uart_dev, usb_tx_drain_buffer, chunk_len);
		if (send_len <= 0)
		{
			return;
		}

		key = irq_lock();
		size_t sent_len = min_size_t((size_t)send_len, chunk_len);
		sent_len = min_size_t(sent_len, usb_tx_ring_count);
		usb_tx_ring_tail = (usb_tx_ring_tail + sent_len) % USB_TX_RING_BUF_SIZE;
		usb_tx_ring_count -= sent_len;
		usb_tx_bytes_sent += (uint32_t)sent_len;
		if (sent_len < chunk_len)
		{
			usb_tx_partial_write_count++;
		}
		irq_unlock(key);

		if (sent_len < chunk_len)
		{
			return;
		}
	}
}

static void interrupt_handler(const struct device *dev, void *user_data) // UART read callback from PC
{
	ARG_UNUSED(user_data);
	while (uart_irq_update(dev) && uart_irq_is_pending(dev))
	{
		/* rx buffer data from PC */
		if (uart_irq_rx_ready(dev)) 
		{
			int recv_len; // number of usb read from PC
			uint8_t Command_from_PC[32];
			recv_len = uart_fifo_read(dev, Command_from_PC, sizeof(Command_from_PC)); 
			if (recv_len < 0)
			{ // cannot read command from PC
				recv_len = 0;
				LOG_INF("Failed to read commands from PC! /n");
			}else{
				process_pc_command_bytes(Command_from_PC, (size_t)recv_len);
			}
			
		}
		if (uart_irq_tx_ready(dev))
		{
			usb_tx_drain_irq(dev);
		}
	}
}

/****************************************esb PRX function********************************************************/
void event_handler(struct esb_evt const *event) // deal the receive event and add payload to ACK
{
	switch (event->evt_id)
	{
	case ESB_EVENT_TX_SUCCESS:
	{
		unsigned int key = irq_lock();
		pending_command_inflight = false;
		pending_command_valid = false;
		irq_unlock(key);
		try_submit_pending_command();
	}
		break;
	case ESB_EVENT_TX_FAILED:
	{
		bool log_drop = false;
		unsigned int key = irq_lock();
		pending_command_inflight = false;
		if (pending_command_valid)
		{
			if (pending_command.retries_left > 0)
			{
				pending_command.retries_left--;
			}
			else
			{
				pending_command_valid = false;
				log_drop = true;
			}
		}
		irq_unlock(key);
		if (log_drop)
		{
			LOG_INF("Failed to send command to peripheral after retries");
		}
		try_submit_pending_command();
	}
		break;
	case ESB_EVENT_RX_RECEIVED:
	{
	while(esb_read_rx_payload(&rx_payload)==0){
			if(rx_payload.length > 0){
				counter_loop++;
				esb_rx_packet_count++;

				// timestamp alignment
				if(rx_payload.data[0] == 0x0300){
					rx_payload.data[rx_payload.length/2] = rx_payload.rssi;
					rx_payload.data[rx_payload.length/2 + 1] = 0x2221;
					rx_payload.data[rx_payload.length/2 + 2] = 0x2423;
					rx_payload.data[rx_payload.length/2 + 3] = 0x2625;
					rx_payload.data[rx_payload.length/2 + 4] = 0x2827;
					(void)usb_tx_enqueue_bytes((const u8_t *)rx_payload.data, rx_payload.length + 10);

					// set channel
					rf_channel = (u8_t)rx_payload.data[5];
					channel_switch=true;
					k_wakeup(mainThread);
					continue;
				}

				if(counter_loop % 500 == 0){
					gpio_pin_toggle_dt(&led);
				}

				memcpy(rx_temp_payload, rx_payload.data, rx_payload.length);
				// define the code to seqarate the individual packages
				// rssi 
				rx_temp_payload[rx_payload.length/2] = rx_payload.rssi;
				rx_temp_payload[rx_payload.length/2 + 1] = 0x2221;
				rx_temp_payload[rx_payload.length/2 + 2] = 0x2423;
			
				esb_packets_length = rx_payload.length + 6;
				bool append_group_end = ((packets_accumul_num + 1) >= USBTX_PACKETS_SIZE);
				if (usb_tx_enqueue_segments((const u8_t *)rx_temp_payload, esb_packets_length,
							    append_group_end ? usb_group_end_marker : NULL,
							    append_group_end ? sizeof(usb_group_end_marker) : 0))
				{
					packets_accumul_num++;
					if (packets_accumul_num >= USBTX_PACKETS_SIZE)
					{
						packets_accumul_num = 0;
					}
				}
			}
		}
	try_submit_pending_command();
	break;
	}
	}
}

int clocks_start(void)
{
	int err;
	int res;
	struct onoff_manager *clk_mgr;
	struct onoff_client clk_cli;


	clk_mgr = z_nrf_clock_control_get_onoff(CLOCK_CONTROL_NRF_SUBSYS_HF);
	if (!clk_mgr)
	{
		printk("Unable to get the Clock manager");
		return -ENXIO;
	}

	sys_notify_init_spinwait(&clk_cli.notify);

	err = onoff_request(clk_mgr, &clk_cli);
	if (err < 0)
	{
		printk("Clock request failed: %d", err);
		return err;
	}

	do
	{
		err = sys_notify_fetch_result(&clk_cli.notify, &res);
		if (!err && res)
		{
			printk("Clock could not be started: %d", res);
			return res;
		}
	} while (err);
	printk("HF clock started");
	return 0;
}

int esb_initialize(void)
{
	int err;
	/* These are arbitrary default addresses. In end user products
	 * different addresses should be used for each set of devices.
	 */
	uint8_t base_addr_0[4] = {0xE7,0xE7,0xE7,0xE7};
	uint8_t base_addr_1[4] = {0xC2, 0xC2, 0xC2, 0xC2};
	uint8_t addr_prefix[8] = {0xE7, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8};

	struct esb_config config = ESB_DEFAULT_CONFIG;

	config.protocol = ESB_PROTOCOL_ESB_DPL;
	config.bitrate = ESB_BITRATE_2MBPS; 
	config.mode = ESB_MODE_PRX;
	config.event_handler = event_handler;
	config.selective_auto_ack = true; // 需要两边的该参数都需要为true 才能使能noack参数
	// config.payload_length = 252;

	err = esb_init(&config);
	if (err)
	{
		return err;
	}

	err = esb_set_base_address_0(base_addr_0);
	if (err)
	{
		return err;
	}

	err = esb_set_base_address_1(base_addr_1);
	if (err)
	{
		return err;
	}

	err = esb_set_prefixes(addr_prefix, ARRAY_SIZE(addr_prefix));
	if (err)
	{
		return err;
	}
	esb_set_tx_power(ESB_TX_POWER_4DBM);

	esb_set_rf_channel(RELAY_DEFAULT_ESB_CHANNEL);
	return 0;
}


/**************************************** main function********************************************************/
int main(void)
{
	int err;
	mainThread = k_sched_current_thread_query();
	/*************LED setup***************/
	if (!gpio_is_ready_dt(&led)) {
                LOG_INF("failed");
		return 0;
	}
	err = gpio_pin_configure_dt(&led, GPIO_OUTPUT_ACTIVE);
	if (err < 0) {
		return 0;
	}

	LED_hinting(200, 10);


	/* ESB setup */
	
	err = clocks_start();
	if (err)
	{
		return;
	}
	err = esb_initialize();
	if (err)
	{
		LOG_INF("ESB initialization failed, err %d", err);
		return;
	}

	err = esb_start_rx();
	if (err)
	{
		printk("RX setup failed, err %d", err);
		return;
	}

	rx_payload.pipe = 0;
	tx_payload.pipe = 0;

	
	/* usb cdc setup */ 
	int ret;

	dev = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
	if (!device_is_ready(dev))
	{
		printk("CDC ACM device not ready");
		return;
	}
	ret = usb_enable(NULL);
	if (ret != 0)
	{
		printk("Failed to enable USB");
		return;
	}

	/* Wait 1 sec for the host to do all settings */
	k_busy_wait(1000000);

	uart_irq_callback_set(dev, interrupt_handler);
	// enable the rx irq to recieve the command
	uart_irq_rx_enable(dev);
	usb_tx_kick();


	// main loop
	while (true){
		if (relay_reboot_requested){
			relay_reboot_requested = false;
			k_sleep(K_MSEC(30));
			sys_reboot(SYS_REBOOT_COLD);
		}

		if(channel_switch){
			channel_switch = false;
			ret = esb_stop_rx();
			ret = esb_set_rf_channel((u8_t)rf_channel);
			ret = esb_start_rx();
			try_submit_pending_command();
		}

		k_sleep(K_FOREVER);
	} // main loop 

	return 0;
}
// 在uart buffer 溢出了之后，就等一段时间再进行发送，以免出现问题
