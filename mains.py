
import sqlite3
import hashlib
import secrets
import os
import hmac
import getpass


DB_NAME = "ATM_System.db"


def connect_db():
    connection = sqlite3.connect(DB_NAME)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        200_000
    )

    return salt.hex() + ":" + password_hash.hex()


def verify_password(password, stored_hash):
    try:
        salt_hex, hash_hex = stored_hash.split(":")
        salt = bytes.fromhex(salt_hex)

        new_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            200_000
        )

        return hmac.compare_digest(new_hash.hex(), hash_hex)

    except (ValueError, TypeError):
        return False


def setup_database():
    with connect_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS Accounts (
                AccountID INTEGER PRIMARY KEY AUTOINCREMENT,
                FullName TEXT NOT NULL,
                Username TEXT NOT NULL UNIQUE,
                PasswordHash TEXT NOT NULL,
                Balance NUMERIC NOT NULL DEFAULT 0,
                CreatedAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS Transactions (
                TransactionID INTEGER PRIMARY KEY AUTOINCREMENT,
                AccountID INTEGER NOT NULL,
                TransactionType TEXT NOT NULL
                    CHECK (TransactionType IN ('Deposit', 'Withdraw')),
                Amount NUMERIC NOT NULL CHECK (Amount > 0),
                TransactionDate TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                BalanceAfter NUMERIC NOT NULL,
                FOREIGN KEY (AccountID)
                    REFERENCES Accounts(AccountID)
            )
        """)


def get_password(prompt):
    import sys
    import msvcrt

    print(prompt, end="", flush=True)
    password = ""

    while True:
        char = msvcrt.getwch()

        if char in ("\r", "\n"):
            print()
            break

        elif char == "\003":
            raise KeyboardInterrupt

        elif char == "\b":
            if password:
                password = password[:-1]
                print("\b \b", end="", flush=True)

        elif char in ("\x00", "\xe0"):
            msvcrt.getwch()

        else:
            password += char
            print("*", end="", flush=True)

    return password

def create_account():
    print("\n--- Create Account ---")

    full_name = input("Enter your full name: ").strip()
    username = input("Choose a username: ").strip()
    password = get_password("Choose a password: ")

    if not full_name or not username or not password:
        print("All fields are required.")
        return

    if len(password) < 8:
        print("Password must be at least 8 characters.")
        return

    password_hash = hash_password(password)

    try:
        with connect_db() as connection:
            connection.execute("""
                INSERT INTO Accounts
                    (FullName, Username, PasswordHash, Balance)
                VALUES (?, ?, ?, ?)
            """, (full_name, username, password_hash, 0))

        print("Account created successfully!")

    except sqlite3.IntegrityError:
        print("That username already exists. Choose another one.")


def login():
    print("\n--- Login ---")

    username = input("Username: ").strip()
    password = get_password("Password: ")

    with connect_db() as connection:
        account = connection.execute("""
            SELECT AccountID, FullName, Username, PasswordHash, Balance
            FROM Accounts
            WHERE Username = ?
        """, (username,)).fetchone()

    if account and verify_password(password, account[3]):
        print(f"\nWelcome, {account[1]}!")
        return account[0]

    print("Incorrect username or password.")
    return None


def account_menu(account_id):
    while True:
        print("\n===== ATM ACCOUNT MENU =====")
        print("1. Check Balance")
        print("2. Deposit Money")
        print("3. Withdraw Money")
        print("4. Transaction History")
        print("5. Logout")

        choice = input("Enter your choice: ").strip()

        if choice == "1":
            with connect_db() as connection:
                account = connection.execute(
            "SELECT Balance FROM Accounts WHERE AccountID = ?",
            (account_id,)
        ).fetchone()
            if account is not None:
                print(f"Your balance is: {float(account[0]):.2f} EGP")
            else:
                print("Account not found.")
    
        elif choice == "2":
            try:
                amount = float(input("Enter deposit amount: "))
                
                
                if amount <= 0:
                    print("Amount must be greater than zero.")
                    continue
                
                with connect_db() as connection:
                    account = connection.execute(
                        "SELECT Balance FROM Accounts WHERE AccountID = ?",
                        (account_id,)
                    ).fetchone()
                    if account is None:
                        print("Account not found.")
                        continue
                    new_balance = float(account[0]) + amount
                    connection.execute(
                        "UPDATE Accounts SET Balance = ? WHERE AccountID = ?",
                        (new_balance, account_id)
                    ) 
                    connection.execute("""
                        INSERT INTO Transactions
                            (AccountID, TransactionType, Amount, BalanceAfter)
                        VALUES (?, 'Deposit', ?, ?)
                    """, (account_id, amount, new_balance))
                print("Deposit successful!")
                print(f"Your account balance is now: {new_balance:.2f} EGP")
            except ValueError:
                print("Please enter a valid number.")
            

        elif choice == "3":
            try:
                amount = float(input("Enter withdrawal amount: "))
                
                if amount <= 0:
                    print("Amount must be greater than zero.")
                    continue
                
                with connect_db() as connection:
                    account = connection.execute(
                        "SELECT Balance FROM Accounts WHERE AccountID = ?",
                        (account_id,)
                    ).fetchone()
                    
                    if account is None:
                        print("Account not found.")
                        continue
                    current_balance = float(account[0])
                    if amount > current_balance:
                        print("Insufficient balance!")
                        continue
                    
                    new_balance = current_balance - amount
                    connection.execute(
                        "UPDATE Accounts SET Balance = ? WHERE AccountID = ?",
                        (new_balance, account_id)
                    )
                    
                    connection.execute("""
                        INSERT INTO Transactions
                            (AccountID, TransactionType, Amount, BalanceAfter)
                        VALUES (?, 'Withdraw', ?, ?)
                    """, (account_id, amount, new_balance))
                    
                print("Withdrawal successful!")
                print(f"Your account balance is now: {new_balance:.2f} EGP")
                
            except ValueError:
                print("Please enter a valid number.")

        elif choice == "4":
            with connect_db() as connection:
                transactions = connection.execute("""
                    SELECT TransactionType, Amount, TransactionDate, BalanceAfter
                    FROM Transactions
                    WHERE AccountID = ?
                    ORDER BY TransactionID DESC
                """, (account_id,)).fetchall()
            if not transactions:
                print("No transactions found.")
            
            else:
                print("\n===== TRANSACTION HISTORY =====")
                
                for transaction in transactions:
                    transaction_type = transaction[0]
                    amount = float(transaction[1])
                    transaction_date = transaction[2]
                    balance_after = float(transaction[3])
                    
                    print(f"\nType: {transaction_type}")
                    print(f"Amount: {amount:.2f} EGP")
                    print(f"Date: {transaction_date}")
                    print(f"Balance after: {balance_after:.2f} EGP")
                    print("-" * 30)
                                                  

        elif choice == "5":
            print("Logged out successfully!")
            break

        else:
            print("Invalid choice(Try again!).")

def main():
    setup_database()

    while True:
        print("\n===== ATM SYSTEM =====")
        print("1. Create Account")
        print("2. Login")
        print("3. Exit")

        choice = input("Choose an option: ").strip()

        if choice == "1":
            create_account()

        elif choice == "2":
            account_id = login()

            if account_id is not None:
              account_menu(account_id)

        elif choice == "3":
            print("Thank you for using ATM System!")
            break

        else:
            print("Invalid choice. Try again.")


if __name__ == "__main__":
    main()
