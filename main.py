# num1 = float(input("Enter your first number."))
# num2 = float(input("Enter your second number."))
# if num1 > num2:
#     print("The first number greater than second number.")
# elif num1 < num2:
#     print("The first number less than second number.")
# else:
#     print("the two numbers are equal.")



# purchase_amount = float(input("Enter Your Parchase_amount: "))
# member = input("Are You a member? (Yes / No):\n")
# if purchase_amount >= 1000 and member == "yes" :
#     discount = purchase_amount * 20 / 100
# elif purchase_amount >= 1000 or member == "yes" :
#     discount = purchase_amount * 10 /100
# else:
#     discount = 0
# total_price = purchase_amount - discount
# print ("Discount: ",discount ,"\nTotal_price: ",total_price)


Balance = 5000
print("Welcome to the ATM!",
          "\n1. Check Balance",
          "\n2. Deposit Money",
          "\n3. Withdraw Money",
          "\n4. Exit")

while True : 
    Choice = input("Enter your choice: ")
    # if Choice == "1":
    #     print("Your balance is:  ",Balance)
        
    if Choice == "2":
        Deposit_amount = float(input("Enter deposit amount: "))
        Balance = Balance + Deposit_amount
        print("Deposit successful!","\nYour account balance is now:  ",Balance)
        
    elif Choice == "3":
        Withdrawal_amount = float(input("Enter withdrawal amount:"))
        Balance = Balance - Withdrawal_amount
        print("Withdrawal successful!","\nYour account balance is now:  ",Balance)
        
    elif Choice == "4":
        print("Thank you for using our ATM!")
        break
    
    else:
        print("Invalid choice")
        continue
                
                
        
    


