obj-m := bbr557.o

all:
	make -C /lib/modules/`uname -r`/build M=$(shell pwd) modules
	sudo insmod bbr557.ko
clean:
	make -C /lib/modules/`uname -r`/build M=$(shell pwd) clean
	sudo rmmod bbr557
