#include "sensor_driver.h"

static bool twim_initialized = false;

/*******************for MP2710 IIC********************/
nrfx_err_t MP2170_twi_data_write(uint8_t slaveAddr, uint8_t regAddr, uint8_t pData){
    uint8_t send[3];
    send[0] = slaveAddr;
    send[1] = regAddr;
    send[2] = pData;

    nrfx_twim_xfer_desc_t twi_desc = NRFX_TWIM_XFER_DESC_TX(slaveAddr, send + 1, 2);
    return nrfx_twim_xfer(&lc_twim, &twi_desc, 0);
}

nrfx_err_t MP2170_twi_data_read(uint8_t slaveAddr, uint8_t regAddr, uint8_t *pData){
    nrfx_err_t err;
    LCDataBuffer[0] = slaveAddr; // write byte
    LCDataBuffer[1] = regAddr;                       // command / register
    LCDataBuffer[2] = LCDataBuffer[0] | 0x1;                // read byte


    nrfx_twim_xfer_desc_t twi_desc = NRFX_TWIM_XFER_DESC_TX(slaveAddr, LCDataBuffer + 1, 1); // 注意： iic 的地址不需要关系读写的位数，地址一般是高7位，驱动会自行配置读写位
    err = nrfx_twim_xfer(&lc_twim, &twi_desc, NRFX_TWIM_FLAG_TX_NO_STOP);
    if (err != NRFX_SUCCESS) {
        return err;
    }

    nrfx_twim_xfer_desc_t twi_desc1 = NRFX_TWIM_XFER_DESC_RX(slaveAddr, LCDataBuffer + 3, 1);
    err = nrfx_twim_xfer(&lc_twim, &twi_desc1, 0);
    if (err != NRFX_SUCCESS) {
        return err;
    }

    *pData = LCDataBuffer[3];

    return NRFX_SUCCESS;
}


/*******************for LC IIC********************/
/* working in blocking mode, Note:TXRX only can work under this non-blocking mode*/

nrfx_err_t LC_twi_data_write(uint8_t slaveAddr, uint8_t regAddr, uint16_t pData)
{
    uint8_t send[5];
    send[0] = slaveAddr * 2;
    send[1] = regAddr;
    send[2] = pData & 0xFF;
    send[3] = pData >> 8;
    send[4] = cal_crc_table(send, 4);

    nrfx_twim_xfer_desc_t twi_desc = NRFX_TWIM_XFER_DESC_TX(slaveAddr, send + 1, 4);
    return nrfx_twim_xfer(&lc_twim, &twi_desc, 0);
}

nrfx_err_t LC_twi_data_read(uint8_t slaveAddr, uint8_t regAddr, uint16_t *pData)
{
    // for LC read uint16 data
    nrfx_err_t err;
    
    LCDataBuffer[0] = slaveAddr * 2; // write byte
    LCDataBuffer[1] = regAddr;                       // command / register
    LCDataBuffer[2] = LCDataBuffer[0] | 0x1;                // read byte


    nrfx_twim_xfer_desc_t twi_desc = NRFX_TWIM_XFER_DESC_TX(slaveAddr, LCDataBuffer + 1, 1); // 注意： iic 的地址不需要关系读写的位数，地址一般是高7位，驱动会自行配置读写位
    err = nrfx_twim_xfer(&lc_twim, &twi_desc, NRFX_TWIM_FLAG_TX_NO_STOP);
    if (err != NRFX_SUCCESS) {
        return err;
    }

    nrfx_twim_xfer_desc_t twi_desc1 = NRFX_TWIM_XFER_DESC_RX(slaveAddr, LCDataBuffer + 3, 3);
    err = nrfx_twim_xfer(&lc_twim, &twi_desc1, 0);
    if (err != NRFX_SUCCESS) {
        return err;
    }

    uint8_t crc = cal_crc_table(LCDataBuffer, 5);
    // CRC failure?
    if (crc != LCDataBuffer[5]) {
        return NRFX_ERROR_INTERNAL;
    }

    *pData = LCDataBuffer[4];
    *pData <<= 8;
    *pData |= LCDataBuffer[3];

    return NRFX_SUCCESS;
}

/* 计算法 CRC */
uint8_t cal_bytes_crc8(uint8_t *data, uint8_t len){ 
    uint8_t crc8 = 0x00;                         /* 计算的初始crc值 */ 
    uint8_t i ;
    while(len--){
        crc8 ^= *data++;                        /* 每次先与需要计算的数据异或,计算完指向下一数据 */  
        for (i = 8; i > 0; --i) {         /* 下面这段计算过程与计算一个字节crc一样 */  
            crc8 = ( crc8 & 0x80 )
              ? (crc8 << 1) ^ 0x07
              : (crc8 << 1);
        }
    }
    return crc8;
}

/* CRC8 查表法计算 */
// 按照多项式 0x07
static const unsigned int crc8_table[256] =
{
  0x00, 0x07, 0x0E, 0x09, 0x1C, 0x1B, 0x12, 0x15, 0x38, 0x3F, 0x36, 0x31, 0x24, 0x23, 0x2A, 0x2D,
	0x70, 0x77, 0x7E, 0x79, 0x6C, 0x6B, 0x62, 0x65, 0x48, 0x4F, 0x46, 0x41, 0x54, 0x53, 0x5A, 0x5D,
	0xE0, 0xE7, 0xEE, 0xE9, 0xFC, 0xFB, 0xF2, 0xF5, 0xD8, 0xDF, 0xD6, 0xD1, 0xC4, 0xC3, 0xCA, 0xCD,
	0x90, 0x97, 0x9E, 0x99, 0x8C, 0x8B, 0x82, 0x85, 0xA8, 0xAF, 0xA6, 0xA1, 0xB4, 0xB3, 0xBA, 0xBD,
	0xC7, 0xC0, 0xC9, 0xCE, 0xDB, 0xDC, 0xD5, 0xD2, 0xFF, 0xF8, 0xF1, 0xF6, 0xE3, 0xE4, 0xED, 0xEA,
	0xB7, 0xB0, 0xB9, 0xBE, 0xAB, 0xAC, 0xA5, 0xA2, 0x8F, 0x88, 0x81, 0x86, 0x93, 0x94, 0x9D, 0x9A,
	0x27, 0x20, 0x29, 0x2E, 0x3B, 0x3C, 0x35, 0x32, 0x1F, 0x18, 0x11, 0x16, 0x03, 0x04, 0x0D, 0x0A,
	0x57, 0x50, 0x59, 0x5E, 0x4B, 0x4C, 0x45, 0x42, 0x6F, 0x68, 0x61, 0x66, 0x73, 0x74, 0x7D, 0x7A,
	0x89, 0x8E, 0x87, 0x80, 0x95, 0x92, 0x9B, 0x9C, 0xB1, 0xB6, 0xBF, 0xB8, 0xAD, 0xAA, 0xA3, 0xA4,
	0xF9, 0xFE, 0xF7, 0xF0, 0xE5, 0xE2, 0xEB, 0xEC, 0xC1, 0xC6, 0xCF, 0xC8, 0xDD, 0xDA, 0xD3, 0xD4,
	0x69, 0x6E, 0x67, 0x60, 0x75, 0x72, 0x7B, 0x7C, 0x51, 0x56, 0x5F, 0x58, 0x4D, 0x4A, 0x43, 0x44,
	0x19, 0x1E, 0x17, 0x10, 0x05, 0x02, 0x0B, 0x0C, 0x21, 0x26, 0x2F, 0x28, 0x3D, 0x3A, 0x33, 0x34,
	0x4E, 0x49, 0x40, 0x47, 0x52, 0x55, 0x5C, 0x5B, 0x76, 0x71, 0x78, 0x7F, 0x6A, 0x6D, 0x64, 0x63,
	0x3E, 0x39, 0x30, 0x37, 0x22, 0x25, 0x2C, 0x2B, 0x06, 0x01, 0x08, 0x0F, 0x1A, 0x1D, 0x14, 0x13,
	0xAE, 0xA9, 0xA0, 0xA7, 0xB2, 0xB5, 0xBC, 0xBB, 0x96, 0x91, 0x98, 0x9F, 0x8A, 0x8D, 0x84, 0x83,
	0xDE, 0xD9, 0xD0, 0xD7, 0xC2, 0xC5, 0xCC, 0xCB, 0xE6, 0xE1, 0xE8, 0xEF, 0xFA, 0xFD, 0xF4, 0xF3
};
 
//采用查表法计算crc代码如下:
uint8_t cal_crc_table(uint8_t *data, uint8_t len){
 
        uint8_t crc8 = 0x00;
 
        while (len --){
                crc8 = crc8_table[crc8 ^ *data++];
        }
        return crc8 ;
}
 

/**
 * @brief Function for handling TWIM driver events.
 *
 * @param[in] p_event Event information structure.
 */
void lc_twim_handler(nrfx_twim_evt_t const * p_event, void * p_context)
{

}
/**
 * @brief init function of twim
 */
nrfx_err_t twim_init (void) // not used as configed by DT
{
    nrfx_err_t err_code;
    err_code = NRFX_SUCCESS;
    if (twim_initialized) {
        nrfx_twim_uninit(&lc_twim);
        twim_initialized = false;
    }
    // 注意：twim 可以直接使用 device tree 进行配置
    nrfx_twim_config_t twim_config1 = NRFX_TWIM_DEFAULT_CONFIG(LC_SCL_PIN ,LC_SDA_PIN);
    twim_config1.frequency          = TWIM_FREQUENCY_FREQUENCY_K400;
    err_code = nrfx_twim_init(&lc_twim, &twim_config1, NULL, NULL);
    if (err_code != NRFX_SUCCESS) {
        return err_code;
    }
    nrfx_twim_enable(&lc_twim); 
    twim_initialized = true;
    return err_code;
}


/*******************for lsm spi********************/
/**
 * @brief merge the register address with the data
 */
void mergeRegisterAndData(uint8_t *pTxBuf, uint8_t regAddr, uint8_t *pData, uint8_t dataLen)
{
    pTxBuf[0] = regAddr;
    memcpy((pTxBuf + 1), pData, dataLen);
}

nrfx_err_t lsm_spim_init(void){
    // spi init
    // nrfx_spim_uninit(&lsm_spi);
    nrfx_err_t err;
    
    nrfx_spim_config_t lsm_spi_init = NRFX_SPIM_DEFAULT_CONFIG(LSM_SCL_PIN, LSM_SDI_PIN, LSM_SDO_PIN, LSM_CS_PIN);
    // lsm_spi_init.frequency = NRFX_MHZ_TO_HZ(8); 
    lsm_spi_init.bit_order = NRF_SPIM_BIT_ORDER_MSB_FIRST;
    lsm_spi_init.mode = NRF_SPIM_MODE_3; // < SCK active low, sample on trailing edge of clock.
    lsm_spi_init.orc = 0x00;

    err = nrfx_spim_init(&lsm_spi, &lsm_spi_init, NULL, NULL); // disable spi interruption
    if (err != NRFX_SUCCESS)
    {
      return err;
    }
    return 0;
}

nrfx_err_t data_write(uint8_t regAddr, uint8_t *pData, uint8_t dataLen, uint8_t device)
{
    // 这里的datalen 是 pData 的datalen ,一般取1 ，单字节的写入寄存器
    
    nrfx_err_t errCode = NRFX_SUCCESS;
    // uint8_t temp_addr = regAddr;
    // temp_addr = temp_addr & 0b01111111;
    mergeRegisterAndData(twimWriteDataBuffer, regAddr, pData, dataLen);
    nrfx_spim_xfer_desc_t xfer_desc = NRFX_SPIM_XFER_TX(twimWriteDataBuffer, dataLen + 1);
	  nrfx_spim_xfer(&lsm_spi, &xfer_desc, 0); 

    return errCode;
}

nrfx_err_t data_read(uint8_t regAddr, uint8_t *pData, uint8_t dataLen, uint8_t device)
{
    uint8_t rx_temp[dataLen + 1];
    uint8_t regbuffer = regAddr | 0x80; // RW value
  // 读取数据的过程为： 先write 一个 reg ，之后再读取data ,其中的pData 为接收数据的buffer 指针 ,dataLen为接收data的长度
    nrfx_err_t errCode = NRFX_SUCCESS;

    nrfx_spim_xfer_desc_t xfer_desc = NRFX_SPIM_XFER_TRX(&regbuffer, 1, rx_temp, dataLen + 1); //注意这里多读了一个字节的无效值要去掉
	  nrfx_spim_xfer(&lsm_spi, &xfer_desc, 0);

    memcpy(pData, (rx_temp + 1), dataLen);

    return errCode;
}

/*******************for bq25176 ********************/
void Charging_stats_init(void){
    nrf_gpio_cfg_input(Bq25176_PG, NRF_GPIO_PIN_PULLUP);
    nrf_gpio_cfg_input(Bq25176_PG_STAT, NRF_GPIO_PIN_PULLUP);
}

void Charging_PG_PG_STAT_get(uint16_t *Charging){
    uint16_t bq25176[2];
    bq25176[0] = nrf_gpio_pin_read(Bq25176_PG);
    bq25176[1] = nrf_gpio_pin_read(Bq25176_PG_STAT);

    *Charging = (int16_t)bq25176[0];
    *Charging = (*Charging * 256) + (int16_t)bq25176[1];

}
